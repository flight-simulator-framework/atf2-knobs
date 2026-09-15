# atf2-knobs

ATF2 IPBSM optimization, beam tuning, linac optimization and synthetic controllers.

## Environment

Use Python >=3.11 and Poetry >=2.0. From this repository, run:

```bash
poetry install
```

Required source repositories: `machine-interfaces`.

## Installation and startup

Use Python 3.11 or newer and Poetry 2 or newer; the local setup was tested with Python 3.13. Keep this repository and `machine-interfaces` in the same parent directory. From this repository, run:

```bash
poetry install
poetry run python Knobs/GUIs/IPBSM_Opt_GUI.py
```
