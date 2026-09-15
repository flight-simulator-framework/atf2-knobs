# atf2-knobs

ATF2 IPBSM optimization, beam tuning, linac optimization and synthetic controllers.

## Environment

Use Python >=3.11 and Poetry >=2.0. From this repository, run:

```bash
poetry install
```

## Installation and startup

Use Python 3.11 or newer and Poetry 2 or newer.

```bash
git clone https://gitlab.cern.ch/flight-simulator/atf2-knobs.git
cd atf2-knobs
poetry install
poetry run python Knobs/GUIs/IPBSM_Opt_GUI.py
```

Poetry downloads and installs the required Python dependencies automatically.
