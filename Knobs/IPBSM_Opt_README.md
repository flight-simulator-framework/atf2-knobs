# IPBSM_Opt (GF / BO / LBO)

## Files
- `IPBSM_Opt.py`  
  Optimizer core (Gaussian fit + bootstrap, BO/LBO using a simple GP, logging, plotting helper)

- `IPBSM_Opt_GUI.py`  
  PyQt6 GUI. Creates output folders under `Data/` and saves PNGs + CSV + JSON.

- `IPBSM_Opt_Synthetic_Controller.py`  
  Test mode controller that generates a synthetic single-peak Gaussian modulation with noise.

- `IPBSM_Opt_run_synthetic_demo.py`  
  Headless smoke test (no GUI).

## Run
```bash
python IPBSM_Opt_GUI.py
```

Test mode:
- Controller = `test`
- Mode: `linear` / `nonlinear2` / `nonlinear4`
- Method: `GF` / `BO` / `LBO`

Machine mode:
- Controller = `machine`
- Requires implementing EPICS in `EPICSIPBSMController` inside `IPBSM_Opt.py`
- The `Get IPBSM` button calls `get_ipbsm()` only in machine mode

## Output
Auto-created directory:
`Data/YYYYmmdd-HHMMSS-(mode)-(method)/`

Inside:
- `measurements.csv`
- `config.json`
- `fit_summary.json`
- `result.json`
- `1D_*.png`, `2D_*_*.png`, `fit_params.txt`
