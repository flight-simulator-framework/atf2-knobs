# atf2-knobs

ATF2 IPBSM optimization, beam tuning, linac optimization and controllers.

## Development

Development, issues and pull requests are hosted on [GitHub](https://github.com/flight-simulator-framework/atf2-knobs). The [CERN GitLab repository](https://gitlab.cern.ch/flight-simulator/atf2-knobs) receives updates from GitHub through a pull mirror.

## Installation and startup

On a control-room computer, install the applications with the deployment script of your machine: [ATF2](https://github.com/flight-simulator-framework/machine-interfaces/blob/master/backends/atf2/atf2_deploy.md). It creates a shared virtual environment and one launcher per GUI, e.g. `./IPBSM_Opt_GUI.sh`.

For a local installation, use Python 3.11 or newer and keep the repositories in one directory. The commands below clone from GitHub; to clone from CERN GitLab instead, replace `https://github.com/flight-simulator-framework/` with `https://gitlab.cern.ch/flight-simulator/`. Building the magnet current–strength conversion library requires `gcc`, `gfortran` and `make`:

```bash
git clone https://github.com/flight-simulator-framework/machine-interfaces.git
git clone https://github.com/flight-simulator-framework/atf2-knobs.git
python3 -m venv venv
source venv/bin/activate
pip install -e machine-interfaces -e machine-interfaces/backends/atf2 -e atf2-knobs
make -C machine-interfaces/backends/atf2/machine_interfaces_atf2/libmagnet libmagnet.so CFLAG=-fPIC
cd atf2-knobs
python Knobs/GUIs/IPBSM_Opt_GUI.py
```
