"""Provenance of measured response matrices and current dispersion observations."""
from datetime import datetime
from pathlib import Path


QM_FILES='QM trim / main response (qm_scan_*.json qm_dispersion_*.json qm_dispersion.json qm_scan.checkpoint.json)'
ZV_FILES='Vertical ZV response (vertical_dispersion*.json dispersion.json)'


def local_time(stamp):
    if not stamp:return 'measurement time unknown'
    try:return datetime.fromisoformat(stamp).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError,ValueError):return 'measurement time unknown'


def measured_time(data):
    stamp=data.get('matrix_measured_at') or (data.get('response_origin') or {}).get('measured_at') or data.get('created_at')
    if stamp:return stamp
    # Legacy ZV files sometimes still contain IOC timestamps for the probe endpoints.
    for frame in reversed(data.get('orbit_measurements',[])):
        if not frame.get('freshness_verified'):continue
        for shot in reversed(frame.get('timestamps',[])):
            if isinstance(shot,dict):
                epochs=[v for v in shot.values() if isinstance(v,(float,int)) and 1.e9<v<1.e10]
                if epochs:return datetime.fromtimestamp(max(epochs)).astimezone().isoformat()
    return None


def matrix_caption(data,kind,path=None,observation=None):
    text=f'{kind} · {local_time(measured_time(data))}' if data else 'No matrix selected'
    if path:text+='\n'+Path(path).name
    if observation:text+='\nBeam measured '+local_time(observation['measured_at'])
    return text


def observation_from_result(data):
    applied=data.get('applied',False)
    state=data.get('applied_baseline') if applied else data.get('baseline')
    if state is None:return None
    options=data['options']
    return dict(baseline=state,names=data['names'],positions_m=data['positions_m'],full_signal_labels=data['full_signal_labels'],
                full_measured=data['full_after' if applied else 'full_measured'],
                measured=data.get('after' if applied else 'measured'),
                valid=data['after_valid'] if applied else data.get('baseline_valid',data.get('acquisition_valid',data['valid'])),
                frames=data.get('after_orbit_measurements' if applied else 'baseline_orbit_measurements',[]),
                frequency_change_khz=options['frequency_change_khz'],momentum_compaction=options['momentum_compaction'],
                tunes=data.get('after_tunes' if applied else 'initial_tunes'),
                evidence=data.get('after_tune_evidence' if applied else 'initial_tune_evidence'),
                measured_at=(data.get('applied_at') if applied else data.get('baseline_measured_at')) or datetime.now().astimezone().isoformat())
