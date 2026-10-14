#!/bin/bash
set -e

cd /userhome/atfop3/KEK-Flight_Simulator
source .flight-simulator-venv/bin/activate

cd cd /userhome/atfop3/flight-simulato-framework/atf2-knobs
python Knobs/Linac_Opt.py
