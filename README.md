# atf2-knobs

ATF2 IPBSM optimization, beam tuning, linac optimization and synthetic controllers.

## Environment

Use Python >=3.11 and Poetry >=2.0. From this repository, run:

```bash
poetry update rf-track --lock
poetry install --extras atf2
```

## Installation and startup

Use Python 3.11 or newer and Poetry 2 or newer.

```bash
git clone https://gitlab.cern.ch/flight-simulator/atf2-knobs.git
cd atf2-knobs
poetry update rf-track --lock
poetry install --extras atf2
poetry run python Knobs/GUIs/IPBSM_Opt_GUI.py
```

Keep these repositories in the same parent directory; Poetry uses their local editable packages. The selected extra installs only that machine adapter and its dependencies.