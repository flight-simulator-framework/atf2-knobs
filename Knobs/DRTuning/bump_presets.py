"""Saved bump transport; runtime checks never build an RFTrack model."""
import copy
import json
from functools import lru_cache
from pathlib import Path
import numpy as np
from .model import name
from .bump_maps import constrained_step,BumpGeometryUnavailable
from scipy.optimize import linprog


@lru_cache(maxsize=1)
def _catalog():
    data=json.loads(Path(__file__).with_name('bump_presets.json').read_text())
    if data.get('schema')!='dr-bump-presets-2':raise ValueError('Unsupported bump preset schema')
    return data


def bump_preset(target):
    data=_catalog();entry=data['presets'].get(name(target))
    return dict(copy.deepcopy(entry),id=data['id']) if entry else None


def bump_targets():
    return list(_catalog()['presets'])


@lru_cache(maxsize=1)
def _responses():
    with np.load(Path(__file__).with_name('bump_preset_responses.npz'),allow_pickle=False) as archive:
        return {target:archive[target] for target in bump_targets()}


def preset_geometry(backend,target,valid,opt,options):
    """Bind a fixed preset to current BPM ordering, readbacks and supply limits."""
    target=name(target);preset=bump_preset(target)
    if preset is None:raise BumpGeometryUnavailable('No saved preset for '+target)
    if backend.unit('correctors')!='A':raise ValueError('Bump presets require corrector currents in A')
    bpms=list(backend.bpm_names);bpm_map={name(n):i for i,n in enumerate(bpms)}
    magnets={name(n):n for n in backend.hcorrectors+backend.vcorrectors}
    excluded={name(n) for n in options.excluded_bpms};fixed={name(n) for n in options.fixed_actuators}
    try:internal=[bpm_map[n] for n in preset['internal_bpms']]
    except KeyError as exc:raise BumpGeometryUnavailable('Preset BPM absent from acquisition: '+str(exc)) from exc
    if any(not valid[i] or name(bpms[i]) in excluded for i in internal):
        raise BumpGeometryUnavailable('Required preset BPM invalid/excluded: '+', '.join(preset['internal_bpms']))
    try:selected=[magnets[n] for n in preset['actuators']]
    except KeyError as exc:raise BumpGeometryUnavailable('Preset corrector absent from machine: '+str(exc)) from exc
    if any(name(n) in fixed for n in selected):raise BumpGeometryUnavailable('Required preset corrector is excluded/fixed')
    outside=np.array([bpm_map[n] for n in preset['outside_bpms'] if n in bpm_map and n not in excluded and valid[bpm_map[n]]],int)
    if len(outside)<4:raise BumpGeometryUnavailable('Insufficient healthy outside BPMs (need at least four)')
    n=len(bpms);reconstruction=np.zeros((4,2*n))
    reconstruction[:,[internal[0],internal[0]+n,internal[1],internal[1]+n]]=preset['reconstruction']
    saved_names=_catalog()['bpm_names'];saved_n=len(saved_names);response=np.zeros((2*n,8))
    saved_response=_responses()[target]
    for j,label in enumerate(saved_names):
        if label in bpm_map:
            i=bpm_map[label];response[[i,i+n]]=saved_response[[j,j+saved_n]]
    centers=np.asarray(backend.read('correctors',selected),float);lower,upper=np.asarray(backend.limits('correctors',selected),float)
    if not np.isfinite(np.r_[centers,lower,upper]).all():raise ValueError('Nonfinite corrector readbacks / limits')
    margin=np.minimum(opt.max_step_A,np.minimum(centers-lower,upper-centers))
    if np.any(margin<=1.e-6):raise BumpGeometryUnavailable('Insufficient current margin for bidirectional corrector probes')
    outrows=np.r_[outside,outside+n];constraint=reconstruction@response
    try:
        knob=constrained_step(response[outrows],np.zeros(len(outrows)),constraint,[0,0,1,0],
                              -margin/opt.calibration_mm,margin/opt.calibration_mm)
    except ValueError as exc:raise BumpGeometryUnavailable('Insufficient calibration current budget: '+str(exc)) from exc
    leakage=np.column_stack((response[outrows],np.zeros(len(outrows))))
    equality=np.column_stack((constraint,-np.array([0.,0.,1.,0.])))
    bounds=list(zip(np.maximum(lower-centers,-opt.max_step_A),np.minimum(upper-centers,opt.max_step_A)))+[(None,None)]
    objective=np.r_[np.zeros(8),1.];predicted=[]
    for sign in (1.,-1.):
        fit=linprog(sign*objective,A_eq=equality,b_eq=np.zeros(4),A_ub=np.r_[leakage,-leakage],
            b_ub=np.full(2*len(outrows),opt.outside_max_mm),bounds=bounds,method='highs')
        if not fit.success:raise BumpGeometryUnavailable('Preset/current limits do not give a feasible scan interval')
        predicted.append(float(fit.x[-1]))
    width=predicted[1]-predicted[0]
    if width<opt.amplitude_mm-1.e-8:
        raise BumpGeometryUnavailable(f'Insufficient current budget: predicted scan width {width:.4g} mm < minimum {opt.amplitude_mm:.4g} mm')
    return dict(target=target,actuators=selected,bpm_names=bpms,positions_m=np.asarray(backend.positions_m).copy(),
        outside_indices=outside,outside_rows=outrows,internal_indices=internal,internal_bpms=[bpms[i] for i in internal],
        reconstruction=reconstruction,model_response=response,model_knob_A_per_mm=knob,centers=centers,limits=[lower,upper],
        source='Saved nominal bump transport; no runtime RFTrack model',model_settings={},model_match_tunes=False,
        selection=dict(method='preset',preset_id=preset['id']),predicted_range_mm=predicted,
        selection_half_range_mm=opt.calibration_mm,
        target_position_note='Target mm inferred from saved nominal transport; corrector response measured during calibration')
