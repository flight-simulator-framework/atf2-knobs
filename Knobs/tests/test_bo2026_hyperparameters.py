"""Offline safety checks for BO2026 fixed hyperparameter presets."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from Knobs.IPBSM_Opt import BaseIPBSMController, Optimizer, OptimizerConfig
from Knobs.Linac_Opt import resolve_linac_length_scale


class _NoopController(BaseIPBSMController):
    def get_ipbsm(self):
        return 0.1, 0.01

    def set_magnet_current(self, name, values):
        pass

    def set_magnet_position(self, name, values):
        pass


class BO2026HyperparameterTests(unittest.TestCase):
    def test_linac_learned_preset_and_safe_fallback(self):
        scale, source = resolve_linac_length_scale(
            "CM1L:phaseWrite", axis_range=16.0, axis_step=1.0, mode="learned_fixed"
        )
        self.assertEqual((scale, source), (4.6, "BO2026-conservative-v1"))

        # Timing is clamped to two hardware steps rather than the 6.91 ns fit.
        timing, _ = resolve_linac_length_scale(
            "EVE_LINAC:OUT0:SetData", axis_range=224.0, axis_step=11.2, mode="learned_fixed"
        )
        self.assertEqual(timing, 22.4)

        legacy, source = resolve_linac_length_scale(
            "developer_axis", axis_range=6.0, axis_step=0.5, mode="learned_fixed"
        )
        self.assertEqual((legacy, source), (2.0, "legacy_fallback_unknown_axis"))

    def test_ipbsm_learned_ay_z_prior(self):
        with tempfile.TemporaryDirectory() as directory:
            optimizer = Optimizer(_NoopController(), self._ay_z_config(), Path(directory))
            metadata = optimizer._gp_metadata()
        self.assertEqual(metadata["length_scales"], {"Ay": 0.08, "Z scan knob": 0.0025})
        self.assertEqual(metadata["zscan_kernel"], "rbf")
        self.assertEqual(metadata["signal_variance"], 0.15)
        self.assertEqual(metadata["noise_variance"], 1e-4)

    def test_z_candidates_are_discrete_unique_and_not_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            optimizer = Optimizer(_NoopController(), self._ay_z_config(), Path(directory))
            optimizer.X = [np.array([0.0, 0.0])]
            candidates = optimizer._candidate_points(80)
        self.assertGreater(len(candidates), 0)
        self.assertTrue(np.all(candidates[:, 1] >= -0.008))
        self.assertTrue(np.all(candidates[:, 1] <= 0.008))
        self.assertTrue(np.allclose(candidates[:, 1] / 0.001, np.round(candidates[:, 1] / 0.001)))
        self.assertEqual(len({tuple(row) for row in candidates}), len(candidates))
        self.assertFalse(any(np.allclose(row, [0.0, 0.0]) for row in candidates))

    def test_z_only_five_point_initial_design(self):
        cfg = OptimizerConfig(
            mode_name="custom", method="BO", acquisition="EI", params=["Z scan knob"],
            bounds={"Z scan knob": (-0.008, 0.008)}, init_sigma={"Z scan knob": 0.004},
            param_steps={"Z scan knob": 0.001}, hyperparameter_mode="learned_fixed",
            zscan_initial_points=5,
        )
        with tempfile.TemporaryDirectory() as directory:
            points = Optimizer(_NoopController(), cfg, Path(directory))._structured_init_points()
        self.assertEqual(sorted(float(point[0]) for point in points), [-0.008, -0.004, 0.0, 0.004, 0.008])

    @staticmethod
    def _ay_z_config() -> OptimizerConfig:
        return OptimizerConfig(
            mode_name="custom",
            method="BO",
            acquisition="EI",
            params=["Ay", "Z scan knob"],
            bounds={"Ay": (-0.2, 0.2), "Z scan knob": (-0.008, 0.008)},
            init_sigma={"Ay": 0.2, "Z scan knob": 0.004},
            param_steps={"Ay": 0.01, "Z scan knob": 0.001},
            hyperparameter_mode="learned_fixed",
            zscan_initial_points=5,
        )


if __name__ == "__main__":
    unittest.main()
