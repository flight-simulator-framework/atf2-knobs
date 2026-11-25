"""Screen vertical correctors with the offline RFTrack matrix and a fresh beam measurement."""
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

import numpy as np

from .model import name


REFERENCE = Path(__file__).with_name('rftrack_nominal_zv_response.npz')


def acquire(service, mode, opt, progress):
    if mode != 'dispersion' or opt.plane != 'y' or opt.selection_method != 'greedy':
        raise ValueError('The saved RFTrack response supports vertical dispersion ZV selection only')
    if opt.model_fix_extraction_orbit or opt.model_fix_extraction_dispersion:
        raise ValueError('Select Current optics for model extraction constraints')
    b = service.backend
    b.validate_mode(mode)
    if b.unit('correctors') != 'A':
        raise ValueError('The nominal ZV response requires corrector currents in A')
    with np.load(REFERENCE, allow_pickle=False) as f:
        bpm_rows = {name(n): i for i, n in enumerate(f['bpm_names'])}
        zv_columns = {name(n): i for i, n in enumerate(f['zv_names'])}
        matrix = np.stack([f['d_dy_mm_per_unit_dp_over_p_per_A'], f['d_cod_y_mm_per_A']])
    if matrix.shape != (2, len(bpm_rows), len(zv_columns)) or not np.isfinite(matrix).all():
        raise ValueError('Invalid nominal RFTrack response dimensions or coefficients')
    indices = [i for i, n in enumerate(b.bpm_names) if name(n) in bpm_rows]
    names = [b.bpm_names[i] for i in indices]
    zv = [n for n in b.vcorrectors if n not in opt.fixed_actuators and name(n) in zv_columns]
    if not zv or not set(names).difference(opt.excluded_bpms):
        raise ValueError('No enabled BPMs or ZVs match the nominal RFTrack matrix')
    nominal = np.asarray(b.target_dispersion('y'), float)
    if nominal.shape != (len(b.bpm_names),) or not np.isfinite(nominal[indices]).all():
        raise ValueError('Invalid vertical dispersion target')
    service.check_cancel()
    baseline = b.snapshot()
    centers = np.asarray(b.read('correctors', zv), float)
    limits = b.limits('correctors', zv)
    service.pause_checkpoint(progress, 0, baseline)
    progress(0, f'Loaded nominal ZV responses. Measuring BPMs at 3 RF settings ({opt.bpm_samples} updates each); ZV currents held fixed.')
    service.orbit_measurements = []
    measured, valid = service._signal(mode, opt)
    service.publish_dispersion_baseline(mode, valid)
    if not b.same_settings(b.snapshot(), baseline):
        raise ValueError('Machine settings changed during dispersion measurement. Find ZVs again.')
    full = service.full_signal.copy()
    rows = [bpm_rows[name(n)] for n in names]
    cols = [zv_columns[name(n)] for n in zv]
    missing_bpms = [n for n in b.bpm_names if name(n) not in bpm_rows]
    missing_zvs = [n for n in b.vcorrectors if n not in opt.fixed_actuators and name(n) not in zv_columns]
    return dict(schema='dr-correction-1', mode=mode, options=asdict(opt), backend_id=b.identity,
                source='Nominal RFTrack ZV response + measured dispersion and COD',
                reference_matrix=dict(file=REFERENCE.name, sha256=sha256(REFERENCE.read_bytes()).hexdigest(),
                                      unmapped_bpms=missing_bpms, unmapped_zvs=missing_zvs),
                names=names, positions_m=np.asarray(b.positions_m)[indices], actuators=zv, kind='correctors',
                actuator_unit='A', baseline=baseline, centers=centers, limits=limits,
                measured=measured[:, indices], target=np.stack([nominal[indices], np.zeros(len(rows))]),
                valid=valid[indices], response=matrix[:, rows][:, :, cols].reshape(2*len(rows), len(cols)),
                full_measured=full[:, indices], signal_labels=['Dispersion','COD'],
                full_signal_labels=['Dispersion X','Dispersion Y','COD X','COD Y'],
                bpm_observations=dict(names=list(b.bpm_names), positions_m=list(b.positions_m),
                                      valid=valid.copy(), full_measured=full),
                orbit_measurements=service.orbit_measurements, probe_history=[], scan_errors=[],
                set_blocked_reason='Measure the selected ZVs before Apply.')
