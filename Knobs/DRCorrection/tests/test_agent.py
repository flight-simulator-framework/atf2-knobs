"""Simulation-only reporting, adoption and restoration contract."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from Knobs.DRCorrection.agent import SimulationCorrectionAgent,default_plan
from Knobs.DRCorrection.backends import DemoBackend,EpicsBackend
from Knobs.DRCorrection.core import Options


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)/'run'
        self.backend=DemoBackend()
        self.options=Options(count_v=2,probe1=self.backend.hcorrectors[0],probe2=self.backend.hcorrectors[6],
                             fixed_actuators=tuple(self.backend.vcorrectors[2:]+self.backend.skews[1:]))

    def events(self):
        return [json.loads(line) for line in (self.directory/'events.jsonl').read_text().splitlines()]

    def test_same_service_runs_three_stages_with_ordered_measured_reports(self):
        agent=SimulationCorrectionAgent(self.backend,self.directory)
        summary=agent.run([(mode,self.options) for mode in ('orbit','dispersion','skew')])
        self.assertEqual(summary['status'],'completed',summary)
        self.assertEqual(len(summary['stages']),3)
        events=self.events()
        self.assertEqual([r['sequence'] for r in events],list(range(1,len(events)+1)))
        for mode in ('orbit','dispersion','skew'):
            phases=[r['event'] for r in events if r['stage']==mode]
            self.assertLess(phases.index('proposal'),phases.index('applying'))
            self.assertLess(phases.index('applying'),phases.index('assessment'))
            data=json.loads((self.directory/(mode+'.json')).read_text())
            self.assertIn('ATFDRRingCorrection.propose_',data['solver'])
        self.assertFalse(summary['learning']);self.assertFalse(summary['emittance_verified'])
        self.assertEqual(summary['equivalent_measurement_seconds'],5*self.backend.acquisitions)

    def test_stop_after_proposal_performs_no_set_and_restores_scan(self):
        before=self.backend.snapshot()
        def subscriber(row):
            if row['event']=='proposal':agent.request_stop()
        agent=SimulationCorrectionAgent(self.backend,self.directory,subscriber)
        with patch.object(agent.service,'apply',wraps=agent.service.apply) as apply:
            summary=agent.run([('orbit',self.options)])
        self.assertEqual(summary['status'],'cancelled');apply.assert_not_called()
        self.assertEqual(self.backend.snapshot(),before)
        self.assertTrue(next(r for r in self.events() if r['event']=='stage_stopped')['restored'])

    def test_stop_during_measurement_restores_probe(self):
        before=self.backend.snapshot()
        original=self.backend.orbit
        def read():
            result=original()
            if self.backend.acquisitions==2:agent.request_stop()
            return result
        self.backend.orbit=read
        agent=SimulationCorrectionAgent(self.backend,self.directory)
        summary=agent.run([('orbit',self.options)])
        self.assertEqual(summary['status'],'cancelled')
        self.assertEqual(self.backend.snapshot(),before)

    def test_intensity_loss_rejects_smaller_orbit_and_restores(self):
        before=self.backend.snapshot();original=self.backend.orbit;candidate=[False]
        def subscriber(row):
            if row['event']=='applying':candidate[0]=True
        def read():
            value=original()
            if candidate[0]:value['tmit']*=.5
            return value
        self.backend.orbit=read
        agent=SimulationCorrectionAgent(self.backend,self.directory,subscriber)
        summary=agent.run([('orbit',self.options)])
        self.assertEqual(summary['status'],'completed')
        self.assertFalse(summary['stages'][0]['accepted'])
        self.assertEqual(summary['stages'][0]['reason'],'BPM intensity decreased')
        self.assertEqual(self.backend.snapshot(),before)

    def test_reporting_failure_after_set_rolls_back(self):
        before=self.backend.snapshot()
        def subscriber(row):
            if row['event']=='assessment':raise RuntimeError('Disconnected report consumer')
        agent=SimulationCorrectionAgent(self.backend,self.directory,subscriber)
        summary=agent.run([('orbit',self.options)])
        self.assertEqual(summary['status'],'failed')
        self.assertIn('Disconnected report consumer',summary['error'])
        self.assertEqual(self.backend.snapshot(),before)

    def test_restoration_failure_remains_visible_and_stops_next_stage(self):
        def subscriber(row):
            if row['event']=='assessment':agent.request_stop()
        agent=SimulationCorrectionAgent(self.backend,self.directory,subscriber)
        with patch.object(agent.service,'reset',side_effect=RuntimeError('restore fault')):
            summary=agent.run([('orbit',self.options),('dispersion',self.options)])
        self.assertEqual(summary['status'],'failed')
        self.assertIn('restore fault',summary['error'])
        self.assertFalse(any(row['stage']=='dispersion' for row in self.events()))

    def test_live_backend_is_rejected_without_io_or_output(self):
        live=EpicsBackend.__new__(EpicsBackend)
        with self.assertRaisesRegex(ValueError,'only DEMO and Stored'):
            SimulationCorrectionAgent(live,self.directory)
        self.assertFalse(self.directory.exists())

    def test_default_plan_handles_sf_before_sd_in_public_catalog(self):
        self.backend.skews=list(reversed(self.backend.skews))
        agent=SimulationCorrectionAgent(self.backend,self.directory)
        summary=agent.run(default_plan(self.backend))
        self.assertEqual(summary['status'],'completed',summary)
        data=json.loads((self.directory/'skew.json').read_text())
        self.assertEqual(len(data['actuators']),1)
        self.assertTrue(data['actuators'][0].startswith('SD'))


if __name__=='__main__':unittest.main()
