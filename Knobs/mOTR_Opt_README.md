# mOTR_Opt

## Overview
- `mOTR_Opt.py`
  Optimizer core for mOTR tuning. It reuses the `IPBSM_Opt` optimizer flow and adds mOTR-specific objective handling, EPICS current control, CSV export, interrupted-run resume, and machine-origin restore.

- `GUIs/mOTR_Opt_GUI.py`
  PyQt6 GUI matching the `IPBSM_Opt` style:
  `Main` / `Config` tabs, `RUNNING` / `DONE!` / `WAITING` knob status, pause-and-resume flow, interrupted-run restart, and live plotting.

- `Knobs/mOTR_measurements.py`
  mOTR measurement helper used by the optimizer. It supports both Conrad-style image analysis and KEK analyzer PV averaging.

## Run
```bash
python3 Knobs/GUIs/mOTR_Opt_GUI.py
```

## Knobs
- `QK1X`, `QK2X`, `QK3X`, `QK4X`
- `QF17X`, `QD18X`, `QF19X`, `QD20X`

All knobs are applied through `currentWrite`.
The optimizer scans offset values around the saved machine-origin current.

Default per-axis config:
- Origin: `0.0 A`
- Half-range: `1.0 A`
- Step: `0.1 A`

## Objectives
- `Conrad`
  Uses the image-analysis path from `mOTR_measurements.py` and takes `sigma_y` from the fitted image result.

- `KEK`
  For each `mOTR0..3`:
  put the target index to `mOTR:analyzer:dispersion:selectedmotr`,
  wait `1 s`,
  insert the screen,
  wait `5 s`,
  read `mOTR:analyzer:size:V` and `mOTR:analyzer:size:H` three times at `1 s` intervals,
  and use the averages as `sigma_y` / `sigma_x`.

Additional saved PVs:
- `mOTR:analyzer:center:H`
- `mOTR:analyzer:center:V`
- `mOTRx:H:x1:Calibration:Factor`
- `mOTRx:V:x1:Calibration:Factor`

The objective minimized by the scan is:
`sqrt(sum_i ((sigma_i / sigma_i_initial)^2))`
for `i = mOTR0..3`.
At the initial condition, the objective is `2`.

## Output
Default save base:
`/atf/data/flight-simulator/mOTR_Opt`

Each run saves:
- `config.json`
- `machine_origin.json`
- `machine_origin-<tag>.json`
- `measurements-<tag>.csv`
- `result.json`
- `measurement_data/measurement-XXXX-<tag>.npz`
- `objective_selected_vs_evaluation.png`
- `objective_compare_vs_evaluation.png` when `Plot Conrad and KEK together` is enabled
- `latest_motr_images.png`

The CSV stores both Conrad and KEK objective data, per-mOTR values, raw KEK sample columns, centers, calibrations, and machine setpoint state.
