import json
import math
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

import numpy as np

from Knobs.RandomExcitation.catalog import BY_NAME, ICT_PVS
from Knobs.RandomExcitation.core import Config, Runner, Sampler, prepare
from Knobs.RandomExcitation.progressive import ProgressiveRange, QualityTrip
from Knobs.RandomExcitation.transport import DemoTransport


class ProgressiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = DemoTransport(period=.003).connect()
        self.dr = 8.
        def tick():
            for n, pv in ICT_PVS.items():
                self.b.publish(pv, self.dr if n == 'DR' else 10.)
        self.b._tick = tick
        tick()
        self.c = Config(max_points=12, samples=2, guard_seconds=0, arrival_timeout=.4,
                        measurement_timeout=.2, end_action='keep', expansion_points=2,
                        expansion_coverage=1, max_changed=2, reference_every=0, joint_every=1)

    def tearDown(self):
        self.b.close()
        self.tmp.cleanup()

    def items(self, names=('L1', 'QA1L')):
        return {n: prepare(BY_NAME[n], self.b, self.c) for n in names}

    def runner(self, names=('L1', 'QA1L')):
        return Runner(self.b, [BY_NAME[n] for n in names], self.c, self.tmp.name)

    def records(self, runner):
        return [json.loads(s) for s in (runner.journal.directory/'commands.jsonl').read_text().splitlines()]

    def test_ten_percent_is_relative_to_initial_ratio_and_baseline_never_drifts(self):
        p = ProgressiveRange(self.items(), self.c)
        p.check_means({'LN0': 10., 'DR': 8.}, True)
        self.assertAlmostEqual(p.snapshot()['minimum_ratio'], .08)
        p.check_means({'LN0': 10., 'DR': .81}, True)
        self.assertEqual(p.baseline, .8)
        with self.assertRaises(QualityTrip):
            p.check_means({'LN0': 10., 'DR': .79}, True)
        with self.assertRaises(QualityTrip):
            p.check_means({'LN0': 10., 'DR': 8.}, False)

    def test_initial_radius_is_one_grid_step_not_percent_of_supply_range(self):
        p = ProgressiveRange(self.items(('L1', 'QA1L', 'Timing')), self.c)
        active = p.active()
        self.assertEqual((active['L1'].lower, active['L1'].upper), (-1., 1.))
        self.assertAlmostEqual(active['QA1L'].lower, 2.9)
        self.assertAlmostEqual(active['QA1L'].upper, 3.1)
        self.assertAlmostEqual(active['Timing'].upper - active['Timing'].origin, 11.2)
        self.c.timing_mode = 'fine'
        p = ProgressiveRange(self.items(('Timing',)), self.c)
        self.assertAlmostEqual(p.active()['Timing'].upper-p.active()['Timing'].origin, 2.8)

    def test_requires_actual_changes_for_every_control_and_excludes_references(self):
        p = ProgressiveRange(self.items(), self.c)
        for kind in ('initial', 'reference', 'quality_recovery', 'pause_remeasure', 'expansion_reference'):
            p.observe(kind, set(p.items))
        p.observe('phase', set())
        self.assertEqual(p.passed, 0)
        for _ in range(20):
            p.observe('phase', {'L1'})
        self.assertFalse(p.ready_to_expand)
        p.observe('qa', {'QA1L'})
        self.assertTrue(p.ready_to_expand)
        self.assertEqual(p.stage, 0)
        self.assertTrue(p.expand_after_reference())
        self.assertEqual(p.stage, 1)
        self.assertEqual(p.passed, 0)

    def test_expansion_and_sampling_never_exceed_user_limits(self):
        items = self.items()
        items['QA1L'] = replace(items['QA1L'], lower=2.8, upper=3.3)
        p = ProgressiveRange(items, self.c)
        sampler = Sampler(items, self.c, p)
        for stage in range(p.max_stage + 1):
            p.stage = stage
            for i in range(1, 60):
                _, values = sampler.next(i)
                for name, value in values.items():
                    self.assertLessEqual(items[name].lower, value)
                    self.assertLessEqual(value, items[name].upper)
                    self.assertLessEqual(abs(value-items[name].origin), items[name].step*2**stage+1e-8)

    def test_off_grid_origin_and_asymmetric_bounds_are_preserved(self):
        self.b.put('QA1L:currentWrite', 3.037)
        item = prepare(BY_NAME['QA1L'], self.b, self.c, (3., 3.2))
        p = ProgressiveRange({'QA1L': item}, self.c)
        rng = np.random.default_rng(2)
        values = {p.active()['QA1L'].random_value(rng) for _ in range(100)}
        self.assertEqual(values, {3., 3.1})
        self.assertEqual(p.items['QA1L'].origin, 3.037)
        with self.assertRaisesRegex(ValueError, 'reference must be inside'):
            ProgressiveRange({'QA1L': replace(item, lower=3.1)}, self.c)

    def test_growth_requires_a_fresh_reference_measurement_in_runner(self):
        r = self.runner()
        self.assertEqual(r.run(), 'complete')
        rows = self.records(r)
        expanded = [i for i, row in enumerate(rows) if row['event'] == 'exploration_range'
                    and row['reason'] == 'expanded_after_reference']
        self.assertTrue(expanded)
        for i in expanded:
            previous = [x for x in rows[:i] if x['event'] == 'measurement_window'][-1]
            self.assertEqual(previous['kind'], 'expansion_reference')
            self.assertEqual(previous['quality'], 'complete')
        last = {'CM1L:phaseWrite': 0., 'QA1L:currentWrite': 3.}
        for pv, value in self.b.commands:
            limit = 1. if pv.startswith('CM') else .1
            self.assertLessEqual(abs(value-last[pv]), limit+1e-8)
            last[pv] = value

    def test_loss_holds_without_recovery_put_and_shrinks_and_caps_range(self):
        r = self.runner()
        def emit(data):
            if data['state'] == 'point' and r.visits == 1:
                r.progressive.stage = 2
                self.dr = .1
                self.b._tick()
            if data['state'] == 'quality_hold':
                self.assertFalse(self.b.commands)
                self.assertEqual(r.progressive.stage, 1)
                self.assertEqual(r.progressive.max_stage, 1)
                r.stop()
        r.emit = emit
        self.assertEqual(r.run(), 'stopped')
        holds = [x for x in self.records(r) if x['event'] == 'quality_hold']
        self.assertEqual(len(holds), 1)
        self.assertFalse(holds[0]['beam_inhibit_sent'])

    def test_recheck_cannot_bypass_low_ratio_and_recovery_remeasures_reference(self):
        r = self.runner()
        holds = []
        def emit(data):
            if data['state'] == 'point' and r.visits == 2:
                self.dr = .1
                self.b._tick()
            if data['state'] == 'quality_hold':
                holds.append(len(self.b.commands))
                if len(holds) == 2:
                    self.dr = 8.
                    self.b._tick()
                if len(holds) > 2:
                    r.stop()
                else:
                    r.resume()
        r.emit = emit
        self.assertEqual(r.run(), 'complete')
        self.assertEqual(len(holds), 2)
        self.assertEqual(holds[0], holds[1])
        self.assertEqual(r.progressive.max_stage, 0)
        self.assertIn('quality_recovery', [x['kind'] for x in self.records(r) if x['event'] == 'measurement_window'])

    def test_requested_initial_return_remains_held_until_quality_recovers(self):
        r = self.runner()
        holds = []
        def emit(data):
            if data['state'] == 'point' and r.visits == 2:
                self.dr = .1
                self.b._tick()
                r.restore_initial_and_end()
            if data['state'] == 'quality_hold':
                holds.append(len(self.b.commands))
                if len(holds) == 2:
                    self.dr = 8.
                    self.b._tick()
                if len(holds) > 2:
                    r.stop()
                else:
                    r.restore_initial_and_end()
        r.emit = emit
        self.assertEqual(r.run(), 'restored_initial')
        self.assertEqual(r.visits, 2)
        self.assertEqual(len(holds), 2)
        self.assertEqual(holds[0], holds[1])
        for p in r.items.values():
            self.assertAlmostEqual(self.b.sample(p.actuator.writes[0]).scalar(), p.origin)

    def test_live_disconnect_stale_zero_and_low_values_trip_before_commands(self):
        r = self.runner()
        r.items = self.items()
        r.progressive = ProgressiveRange(r.items, self.c)
        r.progressive.baseline = .8
        self.b._stop.set()
        self.b._thread.join()
        pv = ICT_PVS['DR']
        for value in (0., -.1, math.nan, .1):
            self.b.publish(pv, value)
            with self.assertRaises(QualityTrip):
                r._check_stop()
        self.b.publish(pv, 8.)
        self.b.on_connection(pvname=pv, conn=False)
        with self.assertRaises(QualityTrip):
            r._check_stop()
        self.b.publish(pv, 8.)
        self.b._samples[pv] = replace(self.b.sample(pv), received=time.monotonic()-3)
        with self.assertRaisesRegex(QualityTrip, 'fresh notification'):
            r._check_stop()
        self.assertFalse(self.b.commands)

    def test_invalid_config_is_rejected_before_channel_writes(self):
        for key, value in [('min_transmission_fraction', 0), ('min_transmission_fraction', 1.1),
                           ('ict_max_age', math.nan), ('expansion_points', 0), ('max_step_steps', 0),
                           ('initial_radius_steps', 0), ('expansion_coverage', 0)]:
            c = replace(self.c, **{key: value})
            with self.assertRaises(ValueError):
                c.validate()


if __name__ == '__main__':
    unittest.main()
