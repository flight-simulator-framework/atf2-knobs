"""QM response acquisition in RF blocks, including selective probe retries and resume."""
import copy
from dataclasses import asdict
import numpy as np

from Knobs.DRCorrection.core import Options
from .dispersion import (PROBE_INCREMENT_A, InsufficientProbeQuality, ProbeRangeUnavailable,
    next_probe_width, probe_floor, probe_pair, probe_scheme, quad_limits, restore_quads)
from .rf_blocks import (ScanFrequency, canonical_blocks, matrix_activity, measure_blocks, nominal_probe_resolved,
                        probe_noise, response_column)


def validate_rf_checkpoint(data,opt):
    """Validate the pending transaction and its raw RF evidence before any resume writes."""
    names=data['actuators']
    pending=data['rf_pending'];results=data['rf_results']
    if set(pending)&set(results) or set(pending)|set(results)!=set(names):
        raise ValueError('Invalid RF block QM selection')
    m=len(data['names'])
    for n,r in results.items():
        if np.shape(r['column'])!=(4*m,) or r['readback']['magnet']!=n:
            raise ValueError('Invalid committed RF column for '+n)
        if not r['readback'].get('skipped') and np.shape(r['valid'])!=(m,):
            raise ValueError('Invalid RF column BPM mask for '+n)
    frames=data['orbit_measurements']
    def check_frame(idx,role,magnet,offset):
        if type(idx)!=int or not 0<=idx<len(frames):raise ValueError('Invalid RF frame index')
        frame=frames[idx]
        if (frame['role'] not in role or frame['magnet']!=magnet or frame['rf_offset_hz']!=offset
                or frame['names']!=data['names'] or frame['nshots']!=10):
            raise ValueError('Saved BPM frame differs from RF probe setting')

    for n,p in pending.items():
        if (not np.isfinite(p['width']) or p['width']<PROBE_INCREMENT_A or
                type(p['attempt'])!=int or not 1<=p['attempt']<=opt.maximum_probe_pairs):
            raise ValueError('Invalid pending probe width or attempt for '+n)
        if 'nominal_round' in p:
            source=p['nominal_round']
            if opt.constrain_cod or type(source)!=int or not 0<=source<len(data['rf_blocks']):
                raise ValueError('Invalid saved nominal response for '+n)
            nominal=data['rf_blocks'][source]['blocks'][0]
            pair=nominal['pairs'][n]
            if nominal['offset_hz']!=data['baseline']['frequency'] or 'error' in pair or len(pair['indices'])!=2:
                raise ValueError('Incomplete saved nominal response for '+n)
            check_frame(nominal['start'],('reference start',),None,nominal['offset_hz'])
            check_frame(nominal['end'],('reference end',),None,nominal['offset_hz'])
            for idx,v in zip(pair['indices'],pair['readbacks'],strict=True):
                check_frame(idx,('probe','probe retry'),n,nominal['offset_hz'])
                if not np.isfinite(v) or frames[idx]['current_A']!=v:
                    raise ValueError('Invalid saved nominal probe for '+n)
    current=data.get('rf_round')
    if current is None:return
    targets=current['targets']
    if set(targets)!=set(pending) or len(current['blocks'])>3:
        raise ValueError('Invalid pending RF block targets')
    for n in targets:
        j=names.index(n)
        expected,_=probe_pair(n,data['centers'][j],np.asarray(data['limits'])[:,j],opt,pending[n]['width'])
        if not np.array_equal(expected,targets[n]):raise ValueError('Saved RF block current pair differs for '+n)
    remaining=set(targets)
    half=opt.frequency_change_khz*500
    blocks=list(current['blocks'])
    partial=current.get('partial')
    if partial is not None:
        if len(blocks)>=3:raise ValueError('Unexpected partial RF block')
        blocks.append(partial)
    order=current.get('rf_order',[0,-1,1])
    if (sorted(order)!=[-1,0,1] or any(type(sign)!=int for sign in order) or
            (order!=[0,-1,1] and not data['rf_blocks'])):
        raise ValueError('Invalid saved RF measurement order')
    for block,sign in zip(blocks,order):
        offset=data['baseline']['frequency']+sign*half
        present=set(block['pairs'])
        if abs(offset-block['offset_hz'])>1.e-6 or not present<=remaining or (block is not partial and present!=remaining):
            raise ValueError('Invalid saved RF block frequency or magnet list')
        check_frame(block['start'],('reference start',),None,block['offset_hz'])
        if block is not partial:check_frame(block['end'],('reference end',),None,block['offset_hz'])
        for n,e in block['pairs'].items():
            if 'error' in e:remaining.remove(n)
            elif len(e['indices'])!=2:raise ValueError('Incomplete saved RF pair for '+n)
            if len(e['indices'])!=len(e['readbacks']) or not np.isfinite(e['readbacks']).all():
                raise ValueError('Invalid saved probe readbacks for '+n)
            for i,v in zip(e['indices'],e['readbacks']):
                check_frame(i,('probe','probe retry'),n,block['offset_hz'])
                if frames[i]['current_A']!=v:raise ValueError('Saved probe readback differs for '+n)


def measure_qms(service,data,opt,progress,event,path):
    reader=service.reader;b=service.backend
    sampling=Options(plane='x',bpm_samples=10,frequency_change_khz=opt.frequency_change_khz,
                     momentum_compaction=opt.momentum_compaction)
    names=data['actuators'];centers=dict(zip(names,data['centers']))
    straight=np.asarray(data['straight'],bool)
    frames=reader.orbit_measurements
    state=data['scan_state']
    results=data.setdefault('rf_results',{})
    pending=data.setdefault('rf_pending',{})
    limits=np.array(data['limits'],float)
    data['limits']=limits
    for n in names:
        if n not in results and n not in pending:
            width,source=service.initial_width(n,data,opt)
            pending[n]=dict(width=width,source=source,attempt=1,failed_width=None)
    def report_columns(stage):
        matrix_activity(reader, columns=(len(results), len(names), len(data['skipped_qms'])),
                        point='', stage=stage, shots=None)

    def selection():
        reader.sync_dispersion_selection('dispersion',opt)
        data['options']['excluded_bpms']=opt.excluded_bpms
        return straight & ~np.isin(data['names'],opt.excluded_bpms)
    def preview(signal,valid):
        event(dict(kind='dispersion',data=dict(names=data['names'],positions_m=data['positions_m'],
            full_measured=signal,full_signal_labels=data['full_signal_labels'],valid=valid,options=data['options'],
            actuators=names,scan_state=dict(completed=state['completed'],common_valid=valid))))
    def nominal_preview(cod,valid):
        if not data['rf_blocks'] and not data.get('resume_checks'):
            preview(np.vstack((np.full_like(cod,np.nan),cod)),valid)
    def baseline(signal,valid):
        if not data['rf_blocks'] and not data.get('resume_checks'):
            data['full_measured']=signal
            data['valid']=valid.copy()
            state['common_valid']=valid.copy()
            preview(signal,valid)
    def sync_columns():
        # Legacy consumers read committed columns in actuator order. A weak earlier
        # magnet may still be pending while later columns are already checkpointed.
        committed=[]
        for n in names:
            if n not in results:break
            committed.append(results[n])
        state.update(completed=len(committed),columns=[r['column'] for r in committed])
        data['probe_readbacks']=[r['readback'] for r in committed]
        data['probe_quality']=[r['quality'] for r in committed]
    def save():
        sync_columns()
        service.checkpoint(data,path,event)
    def skip(n,reason):
        results[n]=dict(column=np.zeros(4*len(straight)),readback=dict(magnet=n,skipped=True,
            requested=[],actual=[],scheme='skipped',reason=reason),quality=dict(skipped=True,measured=False,usable=False,
            reason=reason,signal_rms=None,noise_rms=None,snr=None,orbit_response_rms_mm=None,
            orbit_noise_rms_mm=None,target_change_mm=None))
        data['skipped_qms'].append(dict(magnet=n,reason=reason))
        pending.pop(n);service.successful_widths.pop(n,None)
        progress(0,'SKIP QM probe: '+n+' '+reason)
    def commit_round():
        current=data.pop('rf_round')
        order=current.get('rf_order',[0,-1,1])
        current['blocks']=canonical_blocks(current['blocks'],order)
        data['rf_blocks'].append(current)

    reducing=False
    def acquire_pending(rf):
        nonlocal reducing
        while pending:
            report_columns("Preparing probe currents")
            reader.check_cancel()
            current=data.get('rf_round')
            if current is None:
                pairs={}
                for n in list(pending):
                    j=names.index(n)
                    limits[:,j]=quad_limits(b,n)
                    try:targets,_=probe_pair(n,centers[n],limits[:,j],opt,pending[n]['width'])
                    except ProbeRangeUnavailable as exc:
                        skip(n,str(exc));continue
                    for v in (*targets,centers[n]):b.validate_quadrupole_targets(n,[v])
                    pairs[n]=targets.tolist()
                if not pairs:break
                current=data['rf_round']=dict(targets=pairs,blocks=[])
            pairs=current['targets'];blocks=current['blocks']
            nominal_pairs={}
            nominal_sources=set()
            for n in pairs:
                if 'nominal_round' in pending[n]:
                    source=pending[n]['nominal_round']
                    nominal_sources.add(source)
                    nominal=data['rf_blocks'][source]['blocks'][0]
                    nominal_pairs[n]=nominal['pairs'][n]
            all_cached=len(nominal_sources)==1 and nominal_pairs.keys()==pairs.keys()
            if 'rf_order' not in current and not blocks and 'partial' not in current:
                order=[0,-1,1]
                if rf.moved and not opt.constrain_cod and all(p.get('retry_reason')=='low S/N' for p in pending.values()):
                    order=rf.retry_order(all_cached)
                current['rf_order']=order
            if not blocks and not current.get('partial') and all_cached and current.get('rf_order',[0])[0]==0:
                # All outstanding probes need only the off-energy dispersion slopes.
                nominal=data['rf_blocks'][next(iter(nominal_sources))]['blocks'][0]
                blocks.append(dict(nominal,pairs=nominal_pairs))
            retry_labels={n:f"retry {p['attempt']}, {p.get('retry_reason','probe width')}, {p['width']:g} A"
                          for n,p in pending.items() if p['attempt']>1}

            def write(n,v):
                state['active_magnet']=n
                b.set_quadrupole_current(n,v)
            def restore(n,v):
                restore_quads(b,[n],[v])
                state['active_magnet']=None
            state['status']='probing'
            valid=measure_blocks(reader,sampling,pairs,centers,blocks,write=write,read=b.quadrupole_current,
                restore=restore,selection=selection,strict_loss=opt.adaptive_probe,retry_names=pairs,
                transaction=current,nominal_pairs=nominal_pairs,retry_labels=retry_labels,scan_frequency=rf,rf_order=current.get('rf_order',(0,-1,1)),
                progress=progress,saved=save,baseline_ready=baseline if not data['rf_blocks'] else None,nominal_ready=nominal_preview,
                loss_selection=lambda:~np.isin(data['names'],opt.excluded_bpms))
            report_columns("Checking response columns")
            expected=rf.expected_settings(data['baseline'])
            if not service.settings_match(service.snapshot(data['baseline']['quadrupoles']),expected,names):
                raise ValueError('Settings changed during QM response scan')
            # Column arithmetic and saved readbacks always use nominal, minus, plus.
            order=current.get('rf_order',[0,-1,1])
            blocks=canonical_blocks(blocks,order)
            valid &= np.asarray(state['common_valid'],bool)
            # Endpoint masks from committed columns remain binding after a restart.
            for r in results.values():
                if 'valid' in r:valid &= np.asarray(r['valid'],bool)
            if not (selection() & valid).any():raise ValueError('No common valid straight BPM across RF blocks')
            state['common_valid']=valid.copy()
            reducing=True
            for n,targets in pairs.items():
                p=pending[n];h=p['width']
                entry=dict(magnet=n,half_span_A=h,requested=list(targets),accepted=False)
                data['probe_attempts'].append(entry)
                bad=next((r['pairs'][n] for r in blocks if 'error' in r['pairs'].get(n,{})),None)
                if bad and bad.get('readback_error'):
                    skip(n,bad['error']);continue
                failure=bad['error'] if bad else None
                if not bad:
                    readbacks=[r['pairs'][n]['readbacks'] for r in blocks]
                    # Dispersion and its S/N are measured at the two off-energy settings.
                    span=float(np.mean([hi-lo for lo,hi in readbacks[1:]]))
                    entry['actual']=readbacks[1]
                    if n in nominal_pairs:entry['nominal_reused_from_round']=p['nominal_round']
                    checked=readbacks[1:] if n in nominal_pairs else readbacks
                    if any(hi-lo<=max(1.e-6,(targets[1]-targets[0])*.1) for lo,hi in checked):
                        skip(n,'Unresolved actual QM current span');continue
                    col,mask=response_column(frames,blocks,n,sampling,b)
                    used=valid & selection() & mask
                    if not used.any():failure='No common valid straight BPM'
                    if opt.constrain_cod and used.any():
                        cod=np.asarray(data['full_measured'])[2:,used]
                        for idx in blocks[0]['pairs'][n]['indices']:
                            xy=np.array([frames[idx]['x'],frames[idx]['y']],float)[:,used]
                            if np.max(abs(xy-cod))>opt.orbit_tolerance_mm:failure='Measured COD change exceeds QM tolerance'
                if failure:
                    if not opt.adaptive_probe:raise ValueError(n+': '+failure)
                    p.pop('nominal_round',None)
                    earlier=p['failed_width']
                    p['failed_width']=min(earlier,h) if earlier is not None else h
                    smaller=probe_floor(h/2)
                    entry.update(rejected=True,reason=failure,next_width_A=smaller)
                    if smaller<PROBE_INCREMENT_A or p['attempt']>=opt.maximum_probe_pairs:
                        skip(n,failure);continue
                    p.update(width=smaller,attempt=p['attempt']+1,retry_reason='reduced probe')
                    continue
                signal=float(np.sqrt(np.mean(col[0,used]**2))*span)
                noise=float(np.sqrt(np.mean(probe_noise(frames,blocks,n,sampling,b,'x')[used]**2))*span)
                quality=service.quality_record(signal,noise,int(used.sum()),opt)
                j=names.index(n)
                larger=None
                characterize=opt.characterize_first_scan and p['source']!='previous response S/N estimate'
                if opt.adaptive_probe and not quality['usable'] and p['attempt']<opt.maximum_probe_pairs and not characterize:
                    larger=next_probe_width(h,centers[n],limits[:,j],span,quality,opt,p['failed_width'])
                entry.update(quality=quality,next_width_A=larger,accepted=larger is None)
                if larger is not None:
                    resolved=nominal_probe_resolved(frames,blocks[0]['pairs'][n],valid & mask & ~np.isin(data['names'],opt.excluded_bpms),opt.target_snr,('x','y'))
                    entry['nominal_reuse_qualified']=resolved
                    if resolved and not opt.constrain_cod:p['nominal_round']=len(data['rf_blocks'])
                    else:p.pop('nominal_round',None)
                    p.update(width=larger,attempt=p['attempt']+1,retry_reason='low S/N')
                    continue
                valid &= mask
                service.remember_width(n,data,opt,h,centers[n],limits[:,j],span,quality,p['failed_width'])
                results[n]=dict(column=col.ravel(),valid=mask,quality=quality,readback=dict(magnet=n,requested=targets,
                    actual=readbacks[1],actual_by_rf=readbacks,scheme=probe_scheme(centers[n],targets)))
                del pending[n]
            reducing=False
            state['common_valid']=valid
            commit_round()
            save()
    try:
        data['options']=asdict(opt)
        state['status']='running'
        save()
        rf=ScanFrequency(b)
        if not service.settings_match(service.snapshot(data['baseline']['quadrupoles']),data['baseline'],names):
            raise ValueError('Settings changed before QM response scan')
        try:
            acquire_pending(rf)
        finally:
            try:
                report_columns("Restoring RF")
            finally:
                rf.restore()
        service.check()
        if not service.settings_match(service.snapshot(data['baseline']['quadrupoles']),data['baseline'],names):
            raise ValueError('Settings changed during QM response scan')
        report_columns("Acquisition complete · calculating proposal")
        sync_columns()
        if all(r.get('skipped') for r in data['probe_readbacks']):
            raise ValueError('No measurable QM response: '+'; '.join(r['reason'] for r in data['skipped_qms']))
        data.update(acquisition_valid=np.asarray(state['common_valid'],bool),response=np.column_stack(state['columns']),
            response_column_measured=[not r.get('skipped') for r in data['probe_readbacks']],tune_response=None)
        try:service.solve(data,opt)
        except InsufficientProbeQuality as exc:
            if not opt.characterize_first_scan:raise
            data['proposal_unavailable_reason']=str(exc)
        state.update(status='complete',active_magnet=None)
        data.pop('restored',None)
        save()
        event(dict(kind='dispersion',data=copy.deepcopy(data)))
        progress(100,'QM proposal ready; RF and probe currents restored')
        return data
    except BaseException as exc:
        # If reduction failed partway, some names may already be committed or
        # scheduled at another width. Only the remaining work may be probed.
        if reducing and data.get('rf_round'):
            commit_round()
        data['error']=str(exc);state['status']='interrupted'
        try:data['restored']=bool(service.settings_match(service.snapshot(data['baseline']['quadrupoles']),data['baseline'],names))
        except Exception as restore_error:data.update(restored=False,restoration_error=str(restore_error))
        try:save()
        except Exception as save_error:data['checkpoint_error']=str(save_error)
        raise
