# IPBSM_Opt

## Files
- `IPBSM_Opt.py`  
  Optimizer core (Gaussian fit + bootstrap, BO/LBO using a simple GP, logging, plotting helper)

- `GUIs/IPBSM_Opt_GUI.py`
  PyQt6 GUI. Saves plots, measurements and settings in the configured output directory.

- `IPBSM_Opt_Synthetic_Controller.py`  
  Test mode controller that generates a synthetic single-peak Gaussian modulation with noise.

- `IPBSM_Opt_run_synthetic_demo.py`  
  Headless smoke test (no GUI).

## Run

From the installed `atf2-knobs` environment:

```bash
python -m Knobs.GUIs.IPBSM_Opt_GUI
```

The GUI constructs the ATF2 Ext machine interface. For offline work use the
synthetic controller/demo or the tests; there is no test-controller selector
in this GUI.

START does not open a routine confirmation. If a measurement trigger
(`IPBSM:FringeScan:RemoteScanStart`) has not returned to 0 within 5 seconds,
the interface resets the start/finish PVs and raises a timeout asking the
operator to check the Fringe Scan GUI's **Remote** checkbox. The optimization
pauses with **Resume / Save and End**. This is an error message inferred from
the trigger handshake, not a direct read of the checkbox.
The dialog shows **Please check that the 'Remote' checkbox on the Fringe Scan GUI is ON.**
in large bold text, with **Timeout error** below it and the action buttons at
the bottom. The original error, PV cleanup details, step and knob values are
available under **Show Details**.

## Output

Default directory:
`/atf/data/flight-simulator/IPBSMOpt/YYYY/YYYYmmdd-HHMMSS-ipbsm-bo/`
(Sequential uses the `ipbsm-seq` suffix.) Config → Save directory changes the base path.

Inside:

- `measurements-<timestamp>.csv`
- `config.json`
- `fit_summary.json`
- `result.json`
- Individual history / 1D / 2D plots and fit parameters.

After normal completion or Save and End, the GUI first saves its usual plots,
then automatically writes a single PNG to
`/mnt/nas1/atf-data/op/tempfiles`. All final PNG plots are tiled in two columns,
preserving their aspect ratios. The file name contains the run directory name
and a unique suffix. No CSV, JSON or separate panels are copied there.
There is no confirmation dialog. A missing/unwritable NAS directory reports an
error without removing the normal outputs. If there are no valid measurements,
the run records are saved but no plots or export prompt are produced.

## GP hyperparameter modes

New GUI runs and new `OptimizerConfig` instances default to `learned_fixed`.
Loading a GUI config respects its saved mode; older files without the field
retain `legacy_fixed` for reproducibility.

- `legacy_fixed`: use explicit `gp_ard_length_scales` if present; otherwise
  use the configured per-knob `init_sigma` (falling back to `gp_length_scale`).
- `learned_fixed`: use fixed BO2026-derived length scales: Z-only 0.002;
  when Ay and Z are both selected, Ay 0.08 and Z 0.0025. Z without Ay also
  uses 0.002. Other axes retain `init_sigma` / `gp_length_scale`. All axes
  receive a minimum length scale of two configured knob steps. Explicit
  `gp_ard_length_scales` overrides belong to legacy mode.

Values are in the respective knob's units. In the usual Ay/Ey/Coup2-only
selection, learned mode normally gives the same widths as legacy, except
where the step-size floor applies. Switching modes does not change the
kernel, signal variance (default 0.15), noise variance (default 0.0001),
initial-point count or search bounds. Both modes update the GP predictions
as measurements arrive, but neither re-fits hyperparameters during the run.

## BO progress and EI stopping

The status shows, for example, `step=8/20`. The denominator is the running
configuration's **BO max steps**, including initial points and resumed data.
During `measuring`, the numerator is the evaluation being attempted; after a
measurement it is the recorded evaluation count. Reused points also consume
this existing optimizer budget, while retries do not add an accepted point.
This is an upper limit: EI, operator stop or other enabled conditions can
end a run earlier. Editing Config during a run does not change that run's limit.

A **Total progress** bar below the enlarged status shows the full run budget.
For Sequential, it sums the selected axes' search budgets and credits an axis
fully when its stopping condition is met. For BO, it uses BO max steps and
reaches 100% on EI or other automatic completion. Manual stop preserves partial
progress. Final verification measurements are excluded from the search budget.

Live/result images automatically fit into a grid within the plot area, including
all generated 1D and 2D plots. The layout adapts to plot count and window size
without scrolling. Machine setpoint details remain available in the left panel.

Config → **EI stop rule / EI stop settings** allows editing Threshold and
Consecutive checks. Custom values survive Save config / Load config.
The condition checks the largest EI in the candidate pool after initialization,
before the next measurement. It stops when `max EI <= threshold` for the chosen
number of consecutive checks. A higher EI resets the counter. This condition
applies to EI acquisition, not UCB or the inner scans of Sequential mode.

| Preset | Threshold | Consecutive checks |
| --- | ---: | ---: |
| Aggressive | 0.003 | 2 |
| Standard (GUI default) | 0.001 | 2 |
| Careful | 0.0003 | 3 |

EI uses absolute modulation units, not relative percent: 0.001 corresponds to
0.1 percentage point of modulation. A low EI is a model prediction, not proof
that the true optimum has been reached. The core API's existing default remains
0.0001; GUI settings explicitly override it.

### Historical reference for the provisional threshold (2026-10-22)

Source: `BayesianOptimization_learned_hyperparameters_20260820.xlsx`, sheets
「比較サマリー」「run別推定」, in the local `analysis/BO2026` archive.
These contain 13 Ay+Z runs and 5 Z-only runs from April–June 2026.
For Ay+Z, the median fitted GP noise SD is 0.011289 and the median learned
leave-one-out RMSE is about 0.0267. They are fitted model diagnostics, not
direct instrument resolution or measured gains after stopping.

Start with **0.001 / 2 consecutive checks**: this is about one tenth of the
Ay+Z noise SD and is already the GUI's Standard preset. Use 0.0003 / 3 when
small improvements matter more than measurement time, or consider 0.003 / 2
for earlier termination. These are provisional choices, not a threshold
optimization demonstrated by the archive. Z-only noise estimates vary widely
and some approach the fitting lower bound, so they do not support a universal
noise-derived threshold. The raw candidate-EI histories for these 18 runs were
not available in that archive; missed improvement and time savings at each
threshold have not been established. Confirm with continued measurements
after a proposed stop, under the same GP configuration, before treating a
threshold as validated for operation or for other knob combinations.

## Offline regression checks

```bash
QT_QPA_PLATFORM=offscreen python -m unittest \
  Knobs.tests.test_ipbsm_completion \
  Knobs.tests.test_ipbsm_gui_startup \
  Knobs.tests.test_bo2026_hyperparameters -v
```

These tests replace the machine controller and write exports to temporary
directories. They do not test actual NAS permissions or real-machine operation.
