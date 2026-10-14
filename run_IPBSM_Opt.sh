#!/bin/bash
set -e

cd /userhome/atfop3/KEK-Flight_Simulator
source .flight-simulator-venv/bin/activate

cd /userhome/atfop3/flight-simulato-framework/atf2-knobs/Knobs/GUIs
python IPBSM_Opt_GUI.py
