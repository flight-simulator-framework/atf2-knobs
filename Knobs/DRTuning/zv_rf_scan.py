"""Measured ZV response for the RFTrack shortlist, grouped by RF frequency."""
from dataclasses import asdict
import numpy as np

from Knobs.DRCorrection.core import (EXCITATION_RULE_SOURCE, ZV_RETRY_STEP_A, NoCommonBPMs,
    finite_abs_max, next_excitation_amplitude, validate_adaptive_probe, validate_bpm_sampling)
from .rf_blocks import measure_blocks,reference_signal,response_column


def acquire_zvs(service,opt,progress):
    b=service.backend
    b.validate_mode('dispersion')
    validate_bpm_sampling(opt)
    validate_adaptive_probe(opt)
    nominal=np.asarray(b.target_dispersion('y'),float)
    if nominal.shape!=(len(b.bpm_names),) or not np.isfinite(nominal).all():
        raise ValueError('Invalid nominal vertical dispersion target')
    errors=[]
    baseline=b.measured_snapshot(errors) if hasattr(b,'measured_snapshot') else b.snapshot()
    ignored=[e['magnet'] for e in errors]
    names=[n for n in b.vcorrectors if n not in opt.fixed_actuators and n not in ignored]
    if not names:raise ValueError('No readable, selected ZVs')
    centers={n:float(baseline['correctors'][n]) for n in names}
    limits=np.broadcast_to(np.array(b.limits('correctors',names)).T,(len(names),2))
    bounds=dict(zip(names,limits))
    widths=dict.fromkeys(names,min(opt.corrector_probe_A,opt.adaptive_max_probe) if opt.adaptive_probe else opt.corrector_probe_A)
    if not np.isfinite(list(widths.values())).all() or min(widths.values())<=0:raise ValueError('Positive ZV probe required')
    frames=service.orbit_measurements=[]
    history=service.last_probe_history=[]
    service.last_scan_errors=errors
    rounds=[];columns={};reduced=set();previous_good=set()
    common=np.ones(len(b.bpm_names),bool)
    def selected():
        service.sync_dispersion_selection('dispersion',opt)
        return ~np.isin(b.bpm_names,opt.excluded_bpms)
    def preview(signal,valid):
        service.full_signal=signal
        service.publish_dispersion_baseline('dispersion',valid)
    def restore(n,v):
        if hasattr(b,'restore_probe'):b.restore_probe('correctors',n,v)
        else:b.write('correctors',[n],[v])
    def pause(pct):service.pause_checkpoint(progress,pct,baseline,ignored)
    pause(0)
    max_pairs=opt.adaptive_max_pairs if opt.adaptive_probe else type(opt).adaptive_max_pairs
    for attempt in range(1,int(max_pairs)+1):
        pairs={n:[max(bounds[n][0],centers[n]-w),min(bounds[n][1],centers[n]+w)] for n,w in widths.items()}
        if any(hi<=lo for lo,hi in pairs.values()):raise ValueError('No available ZV probe range')
        blocks=[]
        valid=measure_blocks(service,opt,pairs,centers,blocks,write=lambda n,v:b.write('correctors',[n],[v]),
            read=lambda n:b.read('correctors',[n])[0],restore=restore,selection=selected,strict_loss=False,
            retry_names=set(pairs) if attempt>1 else set(),progress=progress,pause=pause,
            baseline_ready=preview if attempt==1 else None)
        rounds.append(blocks)
        common &= valid
        if attempt==1:
            measured=reference_signal(frames,[r['start'] for r in blocks],opt,b)
        for n,(lo,hi) in pairs.items():
            w=widths.pop(n)
            record=dict(magnet=n,pair=attempt,amplitude=w,unit=b.unit('correctors'),
                        commanded=[lo,hi],used_for_response=False)
            history.append(record)
            failure=next((r['pairs'][n] for r in blocks if 'error' in r['pairs'].get(n,{})),None)
            if failure:
                reason=failure['error']
                if not failure.get('readback_error'):
                    if n in previous_good:raise NoCommonBPMs(f'{n}: {reason} after retry')
                    excursion=abs(failure['failed_current']-centers[n])
                    smaller=ZV_RETRY_STEP_A*max(1,np.floor(excursion/(2*ZV_RETRY_STEP_A)+1.e-10))
                    record.update(status='no_common_bpm',failed_setting_A=failure['failed_current'])
                    if attempt<max_pairs and smaller<excursion-1.e-10:
                        widths[n]=smaller;reduced.add(n);continue
                errors.append(dict(magnet=n,stage='ZV probe',error=reason,restored=True,restoration='baseline restored'))
                continue
            readbacks=[r['pairs'][n]['readbacks'] for r in blocks]
            if min(v[1]-v[0] for v in readbacks)<=1.e-12:
                errors.append(dict(magnet=n,stage='ZV probe',error='Unresolved current span',restored=True,restoration='baseline restored'))
                continue
            col,mask=response_column(frames,blocks,n,opt,b)
            use=common & mask & selected()
            if not use.any():raise NoCommonBPMs(f'{n}: no common enabled BPM for measured response')
            span=readbacks[0][1]-readbacks[0][0]
            observed=finite_abs_max(col[3,use])*span/2
            reached=observed>=opt.adaptive_target_mm*(1-opt.adaptive_target_tolerance)
            record.update(readbacks=readbacks[0],readback_span=span,observed_mm=observed,target_mm=opt.adaptive_target_mm,
                          target_reached=bool(reached),status='target_reached' if reached else 'fixed or limited')
            if opt.adaptive_probe and not reached and n not in reduced and attempt<max_pairs:
                larger=min(opt.adaptive_max_probe,next_excitation_amplitude(w,observed,opt.adaptive_target_mm,opt.adaptive_max_probe))
                endpoints=np.clip([centers[n]-larger,centers[n]+larger],*bounds[n])
                if not np.allclose(endpoints,[lo,hi],rtol=1.e-9,atol=1.e-12):
                    previous_good.add(n);widths[n]=larger;record['status']='retry';continue
            common &= mask
            columns[n]=col
            record['used_for_response']=True
        if not widths:break
    pause(100)
    actual=b.snapshot(excluded=ignored) if ignored else b.snapshot()
    if not b.same_settings(actual,baseline):raise ValueError('Settings changed during ZV response scan')
    names=[n for n in names if n in columns]
    if not names:raise ValueError('No usable measured ZV response')
    target=np.array([nominal,np.zeros_like(nominal)])
    return dict(schema='dr-correction-1',mode='dispersion',options=asdict(opt),backend_id=b.identity,
        names=list(b.bpm_names),positions_m=list(b.positions_m),actuators=names,kind='correctors',
        actuator_unit=b.unit('correctors'),baseline=baseline,centers=np.array([centers[n] for n in names]),
        measured=measured[[1,3]],target=target,response=np.column_stack([columns[n][[1,3]].ravel() for n in names]),
        valid=common,orbit_measurements=frames,rf_blocks=rounds,probe_history=history,scan_errors=errors,
        ignored_readbacks=ignored,set_blocked_reason='',probe_rule_source=EXCITATION_RULE_SOURCE if opt.adaptive_probe else 'fixed amplitude',
        full_measured=measured,full_response=np.column_stack([columns[n].ravel() for n in names]),
        full_signal_labels=['Dispersion X','Dispersion Y','COD X','COD Y'],limits=np.array([bounds[n] for n in names]).T,
        source='measured RF-block response',signal_labels=['Dispersion','COD'])
