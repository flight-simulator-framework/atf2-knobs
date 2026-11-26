"""Measured vertical dispersion response, grouped by RF frequency."""
from dataclasses import asdict
from datetime import datetime,timezone
import numpy as np

from Knobs.DRCorrection.core import (ZV_RETRY_STEP_A, NoCommonBPMs,
    validate_adaptive_probe, validate_bpm_sampling)
from Knobs.DRCorrection.probe_quality import response_quality, wider_probe
from .rf_blocks import (ScanFrequency, canonical_blocks, matrix_activity, measure_blocks, momentum_span, nominal_probe_resolved,
                        probe_noise, reference_signal, response_column)


VERTICAL_ZVS = frozenset(f'ZV{i}R' for i in (*range(1,9), *range(17,33), *range(43,52)))


def eligible_zv(name):
    return name.removeprefix('dr:') in VERTICAL_ZVS


def measure_all_zvs(service,opt,progress):
    if opt.plane!='y' or opt.response_source!='measured' or opt.selection_method!='all':
        raise ValueError('Full ZV measurement requires vertical dispersion and the all-magnet solver')
    data=acquire_zvs(service,opt,progress)
    data['full_zv_candidates']=[n for n in service.backend.vcorrectors if eligible_zv(n)]
    service.check_cancel()
    return service.calculate('dispersion',opt,progress,dataset=data)


def acquire_zvs(service,opt,progress):
    b=service.backend
    b.validate_mode('dispersion')
    validate_bpm_sampling(opt)
    validate_adaptive_probe(opt)
    if opt.adaptive_probe and (opt.bpm_samples<2 or not np.isfinite(opt.dispersion_target_snr) or opt.dispersion_target_snr<=0):
        raise ValueError('Adaptive ZV measurement requires a positive S/N threshold and at least two BPM samples')
    nominal=np.asarray(b.target_dispersion('y'),float)
    if nominal.shape!=(len(b.bpm_names),) or not np.isfinite(nominal).all():
        raise ValueError('Invalid nominal vertical dispersion target')
    errors=[]
    baseline=b.measured_snapshot(errors) if hasattr(b,'measured_snapshot') else b.snapshot()
    ignored=[e['magnet'] for e in errors]
    names=[n for n in b.vcorrectors if eligible_zv(n) and n not in opt.fixed_actuators and n not in ignored]
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
    def report_columns(stage):
        skipped=sum(error.get('stage')=='ZV probe' for error in errors)
        matrix_activity(service, columns=(len(columns)+skipped, len(names), skipped),
                        point='', stage=stage, shots=None)

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
    rf=ScanFrequency(b)
    cached={}
    try:
        for attempt in range(1,int(max_pairs)+1):
            report_columns("Preparing probe currents")
            pairs={n:[max(bounds[n][0],centers[n]-w),min(bounds[n][1],centers[n]+w)] for n,w in widths.items()}
            if any(hi<=lo for lo,hi in pairs.values()):raise ValueError('No available ZV probe range')
            all_cached=bool(cached) and cached.keys()==pairs.keys()
            order=rf.retry_order(all_cached) if rf.moved and previous_good.issuperset(pairs) else [0,-1,1]
            blocks=[dict(rounds[-1][0],pairs=dict(cached))] if all_cached else []
            retry_labels={n:f"retry {attempt}, {'low S/N' if n in previous_good else 'reduced probe'}, {w:g} A"
                          for n,w in widths.items() if attempt>1}
            valid=measure_blocks(service,opt,pairs,centers,blocks,write=lambda n,v:b.write('correctors',[n],[v]),
                read=lambda n:b.read('correctors',[n])[0],restore=restore,selection=selected,strict_loss=False,
                retry_names=pairs,retry_labels=retry_labels,progress=progress,pause=pause,scan_frequency=rf,rf_order=order,nominal_pairs=cached,
                baseline_ready=preview if attempt==1 else None)
            report_columns("Checking response columns")
            blocks=canonical_blocks(blocks,order)
            reused=set(cached);cached.clear()
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
                if min(v[1]-v[0] for v in (readbacks[1:] if n in reused else readbacks))<=max(1.e-6,(hi-lo)*.1):
                    errors.append(dict(magnet=n,stage='ZV probe',error='Unresolved current span',restored=True,restoration='baseline restored'))
                    continue
                col,mask=response_column(frames,blocks,n,opt,b)
                use=common & mask & selected()
                if not use.any():raise NoCommonBPMs(f'{n}: no common enabled BPM for measured response')
                record.update(readbacks=readbacks[0],actual_by_rf=readbacks,readback_span=np.diff(readbacks[0])[0],
                              nominal_reused=n in reused,status='fixed amplitude')
                if opt.adaptive_probe:
                    span=np.mean([v[1]-v[0] for v in readbacks[1:]])
                    signal=span*np.sqrt(np.mean(col[1,use]**2))
                    noise=span*np.sqrt(np.mean(probe_noise(frames,blocks,n,opt,b,'y')[use]**2))
                    quality=response_quality(signal,noise,opt.dispersion_target_snr)
                    orbit_scale=abs(momentum_span(opt,b))
                    record.update(quality=quality,observed_mm=signal*orbit_scale,target_mm=opt.dispersion_target_snr*noise*orbit_scale,
                                  target_reached=quality['usable'],status='S/N reached')
                    if not quality['usable']:
                        larger=wider_probe(w,quality,opt.adaptive_max_probe)
                        endpoints=np.clip([centers[n]-larger,centers[n]+larger],*bounds[n])
                        if n not in reduced and attempt<max_pairs and not np.allclose(endpoints,[lo,hi],rtol=0.,atol=1.e-10):
                            if nominal_probe_resolved(frames,blocks[0]['pairs'][n],use,opt.dispersion_target_snr,('y',)):
                                cached[n]=blocks[0]['pairs'][n]
                            previous_good.add(n);widths[n]=larger;record['status']='retry low S/N'
                            continue
                        reason=f'Dispersion response below S/N {opt.dispersion_target_snr:g} at ±{w:g} A'
                        record['status']=reason
                        errors.append(dict(magnet=n,stage='ZV probe',error=reason,restored=True,restoration='baseline restored'))
                        continue
                common &= mask
                columns[n]=col
                record['used_for_response']=True
            if not widths:break
    finally:
        try:
            report_columns("Restoring RF")
        finally:
            rf.restore()
    pause(100)
    actual=b.snapshot(excluded=ignored) if ignored else b.snapshot()
    if not b.same_settings(actual,baseline):raise ValueError('Settings changed during ZV response scan')
    report_columns("Acquisition complete · calculating proposal")
    names=[n for n in names if n in columns]
    if not names:raise ValueError('No usable measured ZV response: '+errors[-1]['error'])
    target=np.array([nominal,np.zeros_like(nominal)])
    return dict(schema='dr-correction-1',mode='dispersion',options=asdict(opt),backend_id=b.identity,
        names=list(b.bpm_names),positions_m=list(b.positions_m),actuators=names,kind='correctors',
        actuator_unit=b.unit('correctors'),baseline=baseline,centers=np.array([centers[n] for n in names]),
        measured=measured[[1,3]],target=target,response=np.column_stack([columns[n][[1,3]].ravel() for n in names]),
        valid=common,orbit_measurements=frames,rf_blocks=rounds,probe_history=history,scan_errors=errors,
        ignored_readbacks=ignored,set_blocked_reason='',probe_rule_source='Measured vertical dispersion S/N' if opt.adaptive_probe else 'fixed amplitude',
        full_measured=measured,full_response=np.column_stack([columns[n].ravel() for n in names]),
        full_signal_labels=['Dispersion X','Dispersion Y','COD X','COD Y'],limits=np.array([bounds[n] for n in names]).T,
        matrix_measured_at=datetime.now(timezone.utc).isoformat(),source='measured RF-block response',signal_labels=['Dispersion','COD'])
