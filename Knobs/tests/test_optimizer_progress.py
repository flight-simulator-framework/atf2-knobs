"""Run-wide budget weighting and early completion, without machine I/O."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from Knobs import Linac_Opt as linac
from Knobs.IPBSM_Opt import Optimizer
from Knobs.tests.test_ipbsm_completion import FakeController, config as ipbsm_config
from Knobs.optimizer_progress import BudgetProgress


class OverallProgressTests(unittest.TestCase):
    def test_weighted_budget_credits_early_completion_and_does_not_regress(self):
        progress = BudgetProgress({"A": 20, "B": 80})
        self.assertEqual(progress.update("A", 5)["percent"], 5)
        self.assertEqual(progress.update("A", done=True)["percent"], 20)
        self.assertEqual(progress.update("B", 8)["percent"], 28)
        self.assertEqual(progress.update("A", 5)["percent"], 28)
        self.assertEqual(progress.finish()["percent"], 100)

    def test_linac_worker_uses_selected_group_budgets_and_completed_evaluations(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(mode="GROUP_BO", kly_phase=True, grp_L1_4=True, grp_L5_8=False,
                          timing=True, gbo_max_evals_kly_group_1=20, gbo_max_evals_timing=80)
            worker = linac.OptimizationWorker(config, directory, interface=Mock())
            events = []
            worker.progress_signal.connect(events.append)
            self.assertEqual(worker._overall_progress.budgets, {"L1-4": 20, "Timing": 80})
            worker._set_scan_progress("L1-4", 5, 20)
            self.assertEqual(events[-1]["overall_progress"]["percent"], 4)
            worker._emit_progress({"kind": "evaluation", "scan_progress": worker._scan_progress})
            self.assertEqual(events[-1]["overall_progress"]["percent"], 5)
            # EI ends the first group after only five measurements.
            worker._set_scan_progress("L1-4", 5, 20, "final check")
            self.assertEqual(events[-1]["overall_progress"]["percent"], 20)
            worker._set_scan_progress("Timing", 1, 80)
            self.assertEqual(events[-1]["overall_progress"]["percent"], 20)
            worker._emit_progress({"kind": "evaluation", "scan_progress": worker._scan_progress})
            self.assertEqual(events[-1]["overall_progress"]["percent"], 21)
            worker._set_scan_progress("Timing", None, 80, "restoring saved best")
            self.assertEqual(events[-1]["overall_progress"]["percent"], 100)

    def test_linac_refinement_and_combined_timing_are_planned_once(self):
        lbo = linac._run_evaluation_budgets(dict(mode="GROUP_LBO", gun_sol_l0_phase=True,
                                               timing=True, lbo_iters=2, lbo_line_samples=3))
        self.assertEqual(lbo, {"GUN&SOLENOIDE&L0": 25, "Timing": 13})
        budgets = linac._run_evaluation_budgets(dict(mode="SEQUENTIAL", qa=True,
                                                   bo_max_evals=17, bo_refine_evals=10))
        self.assertEqual(budgets, {f"QA{i}L": 27 for i in range(1, 6)})
        config = dict(mode="GROUP_BO", correctors=True, correctors_l8=True, timing=False,
                      timing_fine=True, gbo_max_evals=30, developer_actuators=[
                          {"label": "Timing", "pv_write": linac.TIMING_PV},
                          {"label": "L8", "pv_write": "CM8L:phaseWrite"}])
        budgets = linac._run_evaluation_budgets(config)
        self.assertEqual(budgets, {linac.DEVELOPER_GROUP_NAME: 30, linac.TARGET_LABELS["timing_fine"]: 9})

    def test_ipbsm_axis_completion_and_resume_keep_total_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            cfg = ipbsm_config(method="SEQUENTIAL", params=["Ay", "By"],
                               bounds={"Ay": (-1, 1), "By": (-1, 1)},
                               init_sigma={"Ay": .2, "By": .2}, gf_axis_max_steps=10)
            optimizer = Optimizer(FakeController(), cfg, Path(directory),
                                  progress_cb=lambda step, info: events.append(info))
            optimizer._emit(3, {"phase": "loop", "axis": "Ay", "axis_steps": 3})
            self.assertEqual(events[-1]["overall_progress"]["percent"], 15)
            optimizer._emit(3, {"phase": "axis_done", "axis": "Ay"})
            self.assertEqual(events[-1]["overall_progress"]["percent"], 50)
            optimizer._emit(6, {"phase": "loop", "axis": "By", "axis_steps": 2})
            self.assertEqual(events[-1]["overall_progress"]["percent"], 60)
            # A final-check measurement is not an extra unit of search budget.
            optimizer._emit(7, {"phase": "axis_finalize", "axis": "Ay"})
            self.assertEqual(events[-1]["overall_progress"]["percent"], 60)

    def test_ipbsm_ei_stop_reaches_100_but_user_stop_keeps_partial_progress(self):
        for manual in (False, True):
            with self.subTest(manual=manual), tempfile.TemporaryDirectory() as directory:
                events = []
                optimizer = None
                def receive(step, info):
                    events.append(info)
                    if manual and info.get("phase") == "init":
                        optimizer.stop_flag.request_stop()
                optimizer = Optimizer(FakeController(), ipbsm_config(bo_low_acq_patience=1),
                                      Path(directory), progress_cb=receive)
                with patch.object(optimizer, "_propose_next_BO", return_value=(np.array([.4]), 0.0)):
                    optimizer.run()
                percent = events[-1]["overall_progress"]["percent"]
                if manual:
                    self.assertGreater(percent, 0)
                    self.assertLess(percent, 100)
                else:
                    self.assertEqual(percent, 100)


if __name__ == "__main__":
    unittest.main()
