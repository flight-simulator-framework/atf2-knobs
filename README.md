# atf2-knobs

ATF2 IPBSM optimization, beam tuning, linac optimization and synthetic controllers.

## Installation and startup

On a control-room computer, install the applications with the deployment script of your machine: [ATF2](https://gitlab.cern.ch/flight-simulator/machine-interfaces/-/blob/master/backends/atf2/atf2_deploy.md). It creates a shared virtual environment and one launcher per GUI, e.g. `./IPBSM_Opt_GUI.sh`.

For a local installation, use Python 3.11 or newer and keep the repositories in one directory:

```bash
git clone https://gitlab.cern.ch/flight-simulator/machine-interfaces.git
git clone https://gitlab.cern.ch/flight-simulator/atf2-knobs.git
python3 -m venv venv
source venv/bin/activate
pip install -e machine-interfaces -e machine-interfaces/backends/atf2 -e atf2-knobs
cd atf2-knobs
python Knobs/GUIs/IPBSM_Opt_GUI.py
```

Editable installs pick up `git pull` changes without reinstalling.
