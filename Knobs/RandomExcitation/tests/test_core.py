import ast
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from Knobs.RandomExcitation.catalog import CATALOG, BY_NAME, ICT_PVS, DR_MODE, DR_TURN
from Knobs.RandomExcitation.core import Config, Journal, Prepared, Runner, Sampler, current_bounds, inspect, prepare
from Knobs.RandomExcitation.transport import DemoTransport


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = DemoTransport(period=.004).connect()
        self.config = Config(max_points=4, samples=2, guard_seconds=.005, arrival_timeout=.3,
                             measurement_timeout=.12, end_action="keep")

    def tearDown(self):
        self.b.close()
        self.tmp.cleanup()

    def runner(self, names=("L1", "QA1L"), config=None, emit=None):
        return Runner(self.b, [BY_NAME[n] for n in names], config or self.config, self.tmp.name, emit=emit)

    def records(self, runner):
        return [json.loads(line) for line in (runner.journal.directory / "commands.jsonl").read_text().splitlines()]

    def test_inventory_matches_actual_interfaces_without_importing_them(self):
        root = Path(__file__).resolve().parents[4]
        backend = root / 'machine-interfaces/backends/atf2/machine_interfaces_atf2'
        if not backend.is_dir():
            self.skipTest('Sibling interface sources are unavailable in this installation')
        linac = ast.parse((backend/'InterfaceATF2_Linac.py').read_text())
        dr = ast.parse((backend/'InterfaceATF2_DR.py').read_text())
        def assignment(tree, name):
            for n in ast.walk(tree):
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
                    return ast.literal_eval(n.value)
            self.fail(name)
        mapping = assignment(linac, 'ict_pvs')
        self.assertEqual(set(mapping.values()), set(ICT_PVS.values()))
        self.assertEqual({n for n in assignment(linac, 'LINAC_SEQUENCE') if n.startswith('Z')},
                         {a.name for a in CATALOG if a.group in ('linac', 'bt')})
        self.assertEqual({n for n in assignment(dr, 'sequence') if n.startswith('Z')},
                         {a.name for a in CATALOG if a.group == 'dr'})
        self.assertEqual(len(CATALOG), 163)
        self.assertEqual(len({p for a in CATALOG for p in a.writes}), 163)

    def test_parameter_a_is_lower_b_is_upper_and_fallback_is_explicit(self):
        a = BY_NAME['QA1L']
        self.b.publish(a.limit_pvs[0], -2.3)
        self.b.publish(a.limit_pvs[1], 7.8)
        self.b._metadata.clear()
        self.assertEqual(current_bounds(a, self.b)[:2], (-2.3, 7.8))
        self.b.publish(a.limit_pvs[0], 9)
        with self.assertRaisesRegex(ValueError, 'less than'):
            current_bounds(a, self.b)
        self.b.on_connection(pvname=a.limit_pvs[0], conn=False)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            current_bounds(a, self.b)
        self.b._metadata[a.writes[0]] = dict(lower_ctrl_limit=-1, upper_ctrl_limit=6)
        self.assertEqual(current_bounds(a, self.b), (-1., 6., 'EPICS CTRL (parameter.A/B unavailable)'))

    def test_live_limit_intersection_rejects_inconsistent_metadata(self):
        a = BY_NAME['QA1L']
        self.b._metadata[a.writes[0]] = dict(lower_ctrl_limit=2, upper_ctrl_limit=4)
        self.assertEqual(current_bounds(a, self.b)[:2], (2, 4))
        self.b._metadata[a.writes[0]] = dict(lower_ctrl_limit=20, upper_ctrl_limit=30)
        with self.assertRaisesRegex(ValueError, 'inconsistent'):
            current_bounds(a, self.b)

    def test_quantization_ranges_and_exact_off_grid_restore(self):
        self.b.put('CM1L:phaseWrite', 12.37)
        self.b.put('QA1L:currentWrite', 3.037)
        ready, errors = inspect(self.b, self.config)
        self.assertFalse(errors)
        self.assertEqual((ready['L1'].lower, ready['L1'].upper), (-77.63, 102.37))
        rng = np.random.default_rng(20)
        for name, step in [('L1', 1), ('QA1L', .1)]:
            p = ready[name]
            for _ in range(300):
                value = p.random_value(rng)
                self.assertAlmostEqual(value/step, round(value/step))
                self.assertTrue(p.lower <= value <= p.upper)
            with self.assertRaisesRegex(ValueError, 'off the'):
                p.validate_target(p.origin, self.b)
            p.validate_target(p.origin, self.b, exact_restore=True)
        self.config.end_action = 'initial'
        r = self.runner()
        self.assertEqual(r.run(), 'complete')
        self.assertEqual(self.b.sample('QA1L:currentWrite').scalar(), 3.037)
        self.assertEqual(self.b.sample('CM1L:phaseWrite').scalar(), 12.37)

    def test_phase_wrap_and_ns_timing_grid(self):
        self.b.put('CM1L:phaseWrite', 350)
        self.b._metadata['CM1L:phaseWrite'] = dict(lower_ctrl_limit=0, upper_ctrl_limit=360)
        p = prepare(BY_NAME['L1'], self.b, self.config)
        self.assertEqual(p.encode(410), {'CM1L:phaseWrite': 50.})
        pv = 'EVE_LINAC:OUT0:SetData'
        self.b.put(pv, 1120.37)
        for mode, step, half in [('coarse', 11.2, 112.), ('fine', 2.8, 11.2)]:
            self.config.timing_mode = mode
            p = prepare(BY_NAME['Timing'], self.b, self.config)
            self.assertEqual(p.actuator.unit, 'ns')
            self.assertEqual(p.step, step)
            self.assertAlmostEqual(p.upper - p.origin, half)
            self.assertEqual(p.encode(p.origin + step), {pv: 1120.37 + step})
            for _ in range(50):
                target = p.random_value(np.random.default_rng(_))
                p.validate_target(target, self.b)
                self.assertAlmostEqual((target - p.origin) / step, round((target - p.origin) / step))
        self.b._metadata[pv] = dict(lower_ctrl_limit=1115., upper_ctrl_limit=1125.)
        p = prepare(BY_NAME['Timing'], self.b, self.config)
        self.assertEqual((p.lower, p.upper), (1115., 1125.))
        self.assertEqual([pv for a in CATALOG for pv in a.sources if pv.startswith('EVE_LINAC')],
                         ['EVE_LINAC:OUT0:SetData'])

    def test_sampler_covers_controls_and_never_writes_diagnostics(self):
        items, _ = inspect(self.b, self.config)
        sampler = Sampler(items, self.config)
        seen = set()
        for i in range(1, 501):
            kind, values = sampler.next(i)
            if kind != 'reference':
                self.assertLessEqual(len(values), self.config.max_changed)
                seen.update(values)
                for name, value in values.items():
                    items[name].validate_target(value, self.b)
        self.assertEqual(seen, set(items))
        for name in (DR_MODE, DR_TURN, 'DR:monitors'):
            with self.assertRaises(RuntimeError):
                self.b.put(name, 1)

    def test_joint_points_span_groups_and_duration_terminates(self):
        self.config.max_changed = 2
        self.config.joint_every = 1
        self.config.reference_every = 0
        items, _ = inspect(self.b, self.config)
        sampler = Sampler(items, self.config)
        for i in range(1, 100):
            kind, values = sampler.next(i)
            self.assertEqual(kind, 'joint')
            self.assertEqual(len({items[n].actuator.group for n in values}), 2)
        self.config.max_points = 0
        self.config.hours = .000001
        r = self.runner()
        self.assertEqual(r.run(), 'complete')
        self.assertEqual(r.visits, 1)

    def test_journal_precedes_each_write_and_raw_diagnostics_not_duplicated(self):
        events = []
        r = self.runner(emit=events.append)
        original = self.b.put
        def checked_put(pv, value, timeout=2):
            record = self.records(r)[-1]
            self.assertEqual(record['event'], 'command_intent')
            self.assertEqual((record['pv'], record['requested']), (pv, value))
            original(pv, value, timeout)
        with patch.object(self.b, 'put', checked_put):
            self.assertEqual(r.run(), 'complete')
        records = self.records(r)
        intents = [x for x in records if x['event'] == 'command_intent']
        acks = [x for x in records if x['event'] == 'command_ack']
        self.assertGreater(len(intents), 0)
        self.assertEqual([x['command_id'] for x in intents], [x['command_id'] for x in acks])
        windows = [x for x in records if x['event'] == 'measurement_window']
        self.assertEqual(len(windows), 4)
        self.assertTrue(all('ict_means' not in x for x in windows))
        self.assertTrue(all(x['quality'] == 'complete' for x in windows))
        self.assertTrue(all('waveform' not in x for x in records))
        self.assertEqual(set(x.name for x in r.journal.directory.iterdir()), {'commands.jsonl', 'steps.csv'})

    def test_logging_failure_prevents_the_next_physical_write(self):
        original = Journal.write
        def fail(journal, event, **payload):
            if event == 'command_intent':
                raise OSError('disk full')
            return original(journal, event, **payload)
        with patch.object(Journal, 'write', fail):
            r = self.runner()
            self.assertEqual(r.run(), 'error')
        self.assertEqual(self.b.commands, [])

    def test_command_timeout_marks_unknown_outcome_and_stops(self):
        with patch.object(self.b, 'put', side_effect=TimeoutError('IOC timeout')):
            r = self.runner()
            self.assertEqual(r.run(), 'error')
        records = self.records(r)
        failed = [x for x in records if x['event'] == 'command_error']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['outcome'], 'unknown')
        self.assertFalse(any(x['event'] == 'command_ack' for x in records))

    def test_partial_journal_creation_closes_files_and_never_writes_pvs(self):
        opened = []
        original = Path.open
        def open_file(path, *args, **kwargs):
            if path.name == 'steps.csv':
                raise OSError('storage unavailable')
            stream = original(path, *args, **kwargs)
            opened.append(stream)
            return stream
        with patch.object(Path, 'open', open_file):
            r = self.runner()
            self.assertEqual(r.run(), 'error')
        self.assertTrue(opened)
        self.assertTrue(all(stream.closed for stream in opened))
        self.assertFalse(self.b.commands)

    def test_preflight_rejects_current_override_outside_verified_limits(self):
        r = self.runner()
        r.overrides['QA1L'] = (-20, 20)
        self.assertEqual(r.run(), 'error')
        self.assertFalse(self.b.commands)
        self.assertIsNone(r.journal)

    def test_live_limits_are_rechecked_before_sending(self):
        r = self.runner(names=('QA1L',))
        def emit(data):
            if data['state'] == 'writing' and data['kind'] != 'initial':
                self.b.publish('QA1L:parameter.B', .01)
        r.emit = emit
        self.assertEqual(r.run(), 'error')
        self.assertFalse(self.b.commands)

    def test_repeated_cache_is_not_counted_as_new_samples(self):
        self.b._stop.set()
        self.b._thread.join()
        r = self.runner()
        self.config.max_points = 1
        self.assertEqual(r.run(), 'complete')
        row = next(x for x in self.records(r) if x['event'] == 'measurement_window')
        self.assertEqual(row['quality'], 'incomplete_fresh_ICT_samples')
        self.assertEqual(set(row['sample_counts'].values()), {0})
        self.assertIsNone(r.best)

    def test_zero_beam_is_preserved_and_optional_ict_summary_works(self):
        def zeros():
            for name in ICT_PVS.values():
                self.b.publish(name, 0.)
        self.b._tick = zeros
        self.config.max_points = 1
        self.config.save_ict_summary = True
        r = self.runner()
        self.assertEqual(r.run(), 'complete')
        row = next(x for x in self.records(r) if x['event'] == 'measurement_window')
        self.assertEqual(row['quality'], 'complete')
        self.assertEqual(set(row['ict_means'].values()), {0.})
        self.assertIsNone(r.best)

    def test_missing_readback_does_not_fall_back_to_command(self):
        self.b.on_connection(pvname='CM1L:phaseRead', conn=False)
        r = self.runner()
        self.assertEqual(r.run(), 'error')
        self.assertFalse(self.b.commands)

    def test_external_setting_change_during_measurement_stops(self):
        r = self.runner()
        def emit(data):
            if data['state'] == 'measuring':
                self.b.publish('CM1L:phaseWrite', 7.)
        r.emit = emit
        self.assertEqual(r.run(), 'error')
        records = self.records(r)
        self.assertTrue(any(x['event'] == 'measurement_started' for x in records))
        self.assertFalse(any(x['event'] == 'measurement_window' for x in records))

    def paused_runner(self):
        paused = threading.Event()
        events = []
        r = self.runner()
        def emit(data):
            events.append(data)
            if data['state'] == 'point' and data['visits'] == 2:
                r.request_pause()
            if data['state'] == 'paused':
                paused.set()
        r.emit = emit
        thread = threading.Thread(target=r.run)
        thread.start()
        self.addCleanup(lambda: (r.stop(), thread.join(timeout=2)))
        self.assertTrue(paused.wait(2))
        return r, thread, events

    def test_pause_holds_then_resume_reapplies_and_remeasures_same_state(self):
        self.config.max_points = 3
        r, thread, events = self.paused_runner()
        held = dict(r.state)
        count = len(self.b.commands)
        time.sleep(.03)
        self.assertEqual(len(self.b.commands), count)
        self.b.put('CM1L:phaseWrite', 80)
        r.resume()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        points = [e for e in events if e['state'] == 'point']
        self.assertEqual(points[-1]['kind'], 'pause_remeasure')
        self.assertEqual(points[-1]['settings'], held)
        self.assertEqual(len(points), 3)
        self.assertEqual(self.b.sample('CM1L:phaseWrite').scalar(), held['L1'])

    def test_save_and_end_applies_best_without_another_measurement(self):
        r, thread, events = self.paused_runner()
        best = dict(r.best['settings'])
        visits = r.visits
        r.save_and_end()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(events[-1]['result'], 'saved_best')
        self.assertEqual(r.visits, visits)
        for n, value in best.items():
            self.assertAlmostEqual(self.b.sample(BY_NAME[n].writes[0]).scalar(), value)

    def test_stop_unblocks_pause_without_restoration(self):
        r, thread, events = self.paused_runner()
        count = len(self.b.commands)
        r.stop()
        thread.join(2)
        self.assertEqual(events[-1]['result'], 'stopped')
        self.assertEqual(len(self.b.commands), count)


if __name__ == '__main__':
    unittest.main()
