import copy
import json
import tempfile
from pathlib import Path
import unittest
import numpy as np
from Knobs.DRCorrection.assessment import assess,changed_indices
from Knobs.DRCorrection.backends import DemoBackend
from Knobs.DRCorrection.core import CorrectionService,Options,save_session


class AssessmentTests(unittest.TestCase):
    def fixture(self):
        return dict(mode='orbit',names=['a','b','c'],signal_labels=['COD'],
                    measured=[[3.,4.,100.]],target=[[1.,2.,0.]],predicted=[[2.,3.,0.]],
                    after=[[2.5,3.5,1.e6]],valid=[True,True,True],after_valid=[True,True,False])

    def test_mismatch_uses_common_valid_bpms_and_nonzero_target(self):
        r=assess(self.fixture())['rows'][0]
        self.assertEqual(r['valid_bpms'],2);self.assertEqual(r['excluded_from_comparison'],1)
        self.assertEqual(r['before_rms'],2.);self.assertEqual(r['predicted_rms'],1.)
        self.assertEqual(r['after_rms'],1.5);self.assertEqual(r['improvement_pct'],25.)
        self.assertEqual(r['mismatch_rms'],.5);self.assertEqual(r['mismatch_max'],.5)
        self.assertEqual(r['bpm_mismatch'],[.5,.5,None])
        self.assertEqual(r['mismatch_to_predicted_change'],.5)

    def test_all_invalid_is_unavailable_not_success(self):
        d=self.fixture();d['after_valid']=[False]*3
        r=assess(d)['rows'][0]
        self.assertEqual(r['status'],'unavailable');self.assertIsNone(r['after_rms'])
        self.assertIsNone(r['improvement_pct']);json.dumps(assess(d),allow_nan=False)

    def test_worsening_and_zero_reference_do_not_divide_by_zero(self):
        d=self.fixture();d['after']=[[5.,6.,0.]]
        r=assess(d)['rows'][0];self.assertEqual(r['status'],'worsened');self.assertEqual(r['improvement_pct'],-100.)
        d['measured']=copy.deepcopy(d['target']);d['predicted']=copy.deepcopy(d['target'])
        r=assess(d)['rows'][0];self.assertIsNone(r['improvement_pct']);self.assertIsNone(r['mismatch_to_predicted_change'])

    def test_set_only_writes_changed_magnets_and_saves_verification(self):
        b=DemoBackend();s=CorrectionService(b)
        data=s.calculate('orbit',Options(count_v=3));indices=changed_indices(data)
        calls=[];write=b.write
        def tracked(kind,names,values):calls.append((kind,list(names)));write(kind,names,values)
        b.write=tracked;s.apply(data)
        self.assertEqual(calls,[('correctors',[data['actuators'][i] for i in indices])])
        self.assertLessEqual(len(indices),3)
        self.assertLess(data['assessment']['rows'][0]['mismatch_rms'],1.e-10)
        self.assertEqual(data['applied_actuators'],calls[0][1])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'result.json';save_session(path,data)
            self.assertEqual(json.loads(path.read_text())['assessment']['schema'],'dr-correction-assessment-1')
        new=s.calculate('orbit',Options(count_v=3),dataset=data)
        self.assertNotIn('assessment',new);self.assertNotIn('applied_actuators',new)

    def test_zero_proposal_never_writes(self):
        b=DemoBackend();s=CorrectionService(b);d=s.calculate('orbit',Options());d['delta']=np.zeros(len(d['actuators']))
        b.write=lambda *_:self.fail('zero proposal wrote magnets')
        with self.assertRaisesRegex(ValueError,'No magnet settings change'):s.apply(d)

    def test_dispersion_and_cod_kept_separate(self):
        d=self.fixture();d.update(mode='dispersion',signal_labels=['Dispersion','COD'])
        for key in ('measured','predicted','target','after'):d[key]=d[key]+[[v/10 for v in d[key][0]]]
        rows=assess(d)['rows']
        self.assertEqual(rows[0]['unit'],'mm / (dp/p)');self.assertEqual(rows[1]['unit'],'mm')
        self.assertAlmostEqual(rows[0]['mismatch_rms'],10*rows[1]['mismatch_rms'])


class AssessmentGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def test_count_and_outcome_visible_then_invalidated(self):
        from Knobs.DRTuning.gui import TuningSession
        w=TuningSession(DemoBackend())
        try:
            p=w.panels['orbit'];p.options.count_v=3
            d=w.service.calculate('orbit',p.options);p.display(d)
            n=len(changed_indices(d))
            self.assertIn(f'{n} V correctors',p.proposal_summary.text())
            self.assertEqual(p.form.pushButtonSet.text(),f'Apply {n} magnets (SET)')
            self.assertEqual(sum(p.table.item(i,5).text()=='Will change' for i in range(p.table.rowCount())),n)
            w.service.apply(d);p.display(d)
            self.assertEqual(p.result_tabs.currentIndex(),1);self.assertEqual(p.assessment_table.rowCount(),1)
            self.assertIn('prediction mismatch RMS',p.assessment_summary.text())
            self.assertFalse(p.form.pushButtonSet.isEnabled())
            p.modern_fields['count_v'].setValue(4)
            self.assertIsNone(p.data);self.assertEqual(p.assessment_table.rowCount(),0)
            self.assertIn('No current proposal',p.proposal_summary.text())
        finally:w.close();w.deleteLater();self.app.processEvents()


if __name__=='__main__':unittest.main()
