#!/bin/bash
set -e

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
venv_dir=/userhome/atfop3/KEK-Flight_Simulator/.flight-simulator-venv
if [[ ! -f "$venv_dir/bin/activate" ]]; then
    venv_dir=${VIRTUAL_ENV:-"$HOME/rftrack-env"}
fi
source "$venv_dir/bin/activate"
exec python -m Knobs.DRTuning "$@"
