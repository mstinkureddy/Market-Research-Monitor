"""Standalone fixture authorization, analysis and publication constraints."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import unittest

from monitor_top10.actions.batch import Batch, Control, Permit, Run, FIXTURE_NOW
from monitor_top10.actions.report import Provenance, review, stage
from monitor_top10.fixtures import CUTOFF, HOURS, VENUE, FakeClock, bars, feed_patterns, install_evidence, make_monitor
from monitor_top10.limits import FIXTURE_SYMBOLS
from monitor_top10.synthetic import DataGrant, FixtureAdapter, RESEARCH_ONLY
from monitor_top10.validation import CHECKS


class FixtureEngine(unittest.TestCase):
    def start(self, *, symbols=("F00",), venue=VENUE, **options):
        monitor = make_monitor(**options)
        self.assertTrue(monitor.on(HOURS, CUTOFF, symbols=symbols, authorized=True, venue=venue))
        return monitor

    def verified(self, *, side="C", venue=VENUE, options=None, **evidence):
        monitor = self.start(venue=venue, **(options or {}))
        feed_patterns(monitor, (("F00", side),))
        install_evidence(monitor, side=side, **evidence)
        monitor.clock.advance(seconds=2)
        monitor.tick()
        result, = monitor.results
        return result

    def test_local_off_no_approval_warm_start_or_oversize_scope(self):
        monitor = make_monitor()
        self.assertFalse(monitor.active)
        self.assertFalse(monitor.ingest(bars("F00", monitor.clock.now())[0]))
        self.assertFalse(monitor.on(HOURS, CUTOFF, symbols=("F00",)))
        for symbols in ((), ("F00", "F00"), ("F03",), (*FIXTURE_SYMBOLS, "F03")):
            self.assertFalse(monitor.on(HOURS, CUTOFF, symbols=symbols, authorized=True))
        self.assertFalse(monitor.on(HOURS, CUTOFF, symbols=("F00",), authorized=True, lease=timedelta(minutes=21)))
        self.assertEqual(monitor.feed.calls, [])

    def test_local_scope_rejects_other_bars_and_clears_pending_on_off(self):
        monitor = self.start(symbols=FIXTURE_SYMBOLS)
        self.assertEqual(monitor.universe.screened, 30)
        self.assertFalse(monitor.ingest(bars("F03", monitor.clock.now())[0]))
        feed_patterns(monitor, (("F00", "C"), ("F01", "P"), ("F02", "C")))
        self.assertEqual((len(monitor.in_flight), len(monitor.pending)), (2, 1))
        monitor.off()
        monitor.clock.advance(seconds=2)
        monitor.tick()
        self.assertFalse(monitor.active or monitor.in_flight or monitor.pending or monitor.results or monitor.feed.calls)
        self.assertFalse(make_monitor().active)

    def test_fixture_cutoff_and_lease(self):
        for limit, expected in (({"cutoff": 1, "lease": 9}, "cutoff"), ({"cutoff": 9, "lease": 1}, "lease expired")):
            with self.subTest(expected=expected):
                monitor = make_monitor()
                close = monitor.clock.now() + timedelta(seconds=limit["cutoff"])
                self.assertTrue(monitor.on(HOURS, close, symbols=("F00",), authorized=True,
                                           lease=timedelta(seconds=limit["lease"])))
                monitor.clock.advance(seconds=1)
                monitor.tick()
                self.assertFalse(monitor.active)
                self.assertIn(expected, monitor.audit[-1].detail)

    def test_fixture_entitlement_licensing_freshness_and_scope_fail_closed(self):
        grant = DataGrant("SYNTHETIC_FIXTURE", True, True, "offline synthetic monitor", HOURS.closes)
        for invalid in (replace(grant, source="LIVE"), replace(grant, entitled=False),
                        replace(grant, license_confirmed=False), replace(grant, expires=FIXTURE_NOW)):
            result = FixtureAdapter(invalid).analyze(("F00",), FIXTURE_NOW, HOURS.closes, VENUE)
            self.assertEqual((result["status"], result["results"]), ("BLOCKED", []))
        self.assertEqual(FixtureAdapter(grant).analyze(("F03",), FIXTURE_NOW, HOURS.closes, VENUE)["status"], "BLOCKED")
        stale = FixtureAdapter(grant, quote_age=16).analyze(("F00",), FIXTURE_NOW, HOURS.closes, VENUE)["results"][0]
        self.assertIsNone(stale["quote_observed"])
        self.assertNotIn("contract", stale)

    def test_confirmed_calls_and_puts_remain_no_trade(self):
        for side in ("C", "P"):
            with self.subTest(side=side):
                item = self.verified(side=side)
                self.assertEqual((item.signal.side, item.level, item.decision), (side, 1, "NO TRADE"))
                self.assertEqual(tuple(c.name for c in item.checks), CHECKS)
                self.assertEqual(item.economics.loss, Decimal("31.15"))
                self.assertGreaterEqual(item.economics.reward_risk, Decimal("1.5"))

    def test_failed_retest_cannot_advance(self):
        monitor = self.start()
        feed_patterns(monitor, (("F00", "C"),), fake=True)
        monitor.clock.advance(seconds=2)
        monitor.tick()
        self.assertFalse(monitor.in_flight or monitor.results or monitor.feed.calls)

    def test_fixture_read_loss_and_call_cap_retain_completed_stages(self):
        for options in ({"lose_after": "chain"}, {"max_calls": 2}):
            with self.subTest(options=options):
                result = self.verified(options=options)
                self.assertEqual(result.steps, ("expirations", "chain"))
                self.assertEqual((result.decision, result.modeled_quantity), ("NO TRADE", 0))

    def test_unverified_pricing_terms_costs_and_catalysts_block(self):
        cases = (({"age": 16}, "stale"), ({"bad_quote": "crossed"}, "crossed"),
                 ({"bad_quote": "incomplete"}, "displayed size"), ({"terms_issue": "adjusted"}, "OCC terms"),
                 ({"fee_issue": "zero"}, "fees unverified"), ({"catalyst": True}, "catalyst"))
        for args, detail in cases:
            with self.subTest(args=args):
                result = self.verified(**args)
                self.assertIn(detail, result.reason)
                self.assertFalse(result.display_eligible)
                self.assertEqual(result.modeled_quantity, 0)
        self.assertEqual(self.verified(venue=None).decision, "CONDITIONAL · NO TRADE")

    def test_reward_risk_and_budget_do_not_permit_contracts(self):
        declined = self.verified(delta=".04")
        self.assertLess(declined.economics.reward_risk, Decimal("1.5"))
        self.assertFalse(declined.display_eligible)
        for ask, delta, limit in ((".60", ".55", 50), ("1.08", ".90", 100)):
            with self.subTest(ask=ask):
                result = self.verified(ask=ask, delta=delta)
                self.assertEqual((result.level, result.decision, result.modeled_quantity), (1, "NO TRADE", 0))
                self.assertGreater(result.economics.loss, limit)
                self.assertEqual(result.budget_excess, result.economics.loss - 50)

    def test_report_completed_after_permit_expiry_is_withheld(self):
        clock = FakeClock(FIXTURE_NOW)
        now = clock.now()
        run = Run("9", 1, "a" * 40, "workflow_dispatch", now + timedelta(minutes=20))
        permit = Permit(run.run_id, run.commit, ("F00",), now, now + timedelta(minutes=1))
        batch = Batch(run, clock, lambda: Control(True, now, True, permit))
        self.assertEqual(batch.execute().status, "COMPLETED")
        completed = now + timedelta(minutes=2)
        proof = Provenance(run, True, True, "success", completed, False, permit)
        self.assertEqual(review(stage(batch), proof, completed).status, "UNVERIFIED")

    def test_schema_rejects_nonzero_contracts_and_unapproved_fields(self):
        clock = FakeClock(FIXTURE_NOW)
        now = clock.now()
        run = Run("4", 1, "a" * 40, "workflow_dispatch", now + timedelta(minutes=20))
        permit = Permit(run.run_id, run.commit, ("F00",), now, run.job_deadline)
        batch = Batch(run, clock, lambda: Control(True, now, True, permit))
        self.assertEqual(batch.execute().status, "COMPLETED")
        raw = stage(batch)
        proof = Provenance(run, True, True, "success", now, False, permit)
        record = json.loads(raw)
        self.assertEqual(record["permitted_contracts"], 0)
        ok = review(raw, proof, now)
        for marker in (RESEARCH_ONLY, "NO TRADE", "Permitted contracts: 0"):
            self.assertIn(marker, ok.dashboard)
        for bad in (record | {"permitted_contracts": 1}, record | {"permitted_contracts": False},
                    record | {"unexpected": "extra"},
                    record | {"rows": [record["rows"][0] | {"unexpected": "extra"}]}):
            with self.subTest(keys=tuple(bad.keys())):
                result = review(json.dumps(bad).encode(), proof, now)
                self.assertEqual(result.status, "UNVERIFIED")
                self.assertIn(RESEARCH_ONLY, result.dashboard)
