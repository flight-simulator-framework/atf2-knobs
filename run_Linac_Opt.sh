#!/bin/bash
set -e

cd /userhome/atfop3/KEK-Flight_Simulator
source .flight-simulator-venv/bin/activate

cd /userhome/atfop3/flight-simulator-framework/atf2-knobs/Knobs
python Linac_Opt.py
