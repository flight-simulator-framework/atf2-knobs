"""Measure dispersion responses with a shared RF setting for all current probes."""
import numpy as np

from Knobs.DRCorrection.backends import MagnetReadbackError


def measure_blocks(reader, sampling, pairs, centers, blocks, *, write, read, restore,
                   selection, strict_loss, retry_names, progress, saved=lambda:None,
                   pause=None, baseline_ready=None, nominal_ready=None, loss_selection=None,
                   transaction=None, nominal_pairs=None, retry_labels=None):
    """Track restored QM pairs within the current RF block for resume. Frame indices refer to the raw orbit log."""
    backend=reader.backend
    origin=backend.frequency()
    rf_state=backend.frequency_state()
    half=sampling.frequency_change_khz*500
    if not np.isfinite([half,sampling.momentum_compaction]).all() or min(half,sampling.momentum_compaction)<=0:
        raise ValueError('Positive RF span and momentum compaction required')
    backend.validate_frequency_targets([origin-half,origin+half])
    moved=False
    frames=reader.orbit_measurements

    def sample(label, magnet=None, current=None):
        reader._orbit(sampling)
        frames[-1].update(rf_offset_hz=offset,role=label,magnet=magnet,current_A=current)
        return len(frames)-1

    def read_current(n):
        value=float(read(n))
        if not np.isfinite(value):raise MagnetReadbackError([n],f'Invalid current readback: {n}')
        return value

    def mask(index):
        return np.asarray(frames[index]['valid'],bool)

    def reference(label, required, magnet=None, current=None):
        idx=sample(label,magnet,current)
        if transaction is not None and not (mask(idx) & required & selection()).any():
            progress(phase*30,f'RF {shift/1000:+g} kHz: retry {label} in 3 s')
            if reader.cancel.wait(3.):reader.check_cancel()
            idx=sample(label,magnet,current)
        return idx

    common=np.ones(len(backend.bpm_names),bool)
    for block in blocks:
        common &= mask(block['start']) & mask(block['end'])
    try:
        for phase,shift in enumerate((0.,-half,half)):
            if phase<len(blocks):continue
            reader.check_cancel()
            offset=origin+shift
            if shift:
                moved=True
                backend.set_frequency(offset)
            progress(phase*30,f'RF {shift/1000:+g} kHz: reference orbit')
            partial=transaction.get('partial') if transaction is not None else None
            if partial is None:
                start=reference('reference start',common)
                block=dict(offset_hz=offset,start=start,pairs={})
            else:
                block=partial
                start=block['start']
            active=common & mask(start)
            if not (active & selection()).any():raise ValueError('No common enabled BPMs at reference current')
            if phase==0 and nominal_ready:nominal_ready(orbit_xy(frames[start]),active.copy())
            if phase==2 and baseline_ready:
                baseline_ready(reference_signal(frames,[b['start'] for b in blocks]+[start],sampling,backend),active.copy())
            if transaction is not None:transaction['partial']=block
            for i,(n,targets) in enumerate(pairs.items()):
                if n in block['pairs'] or any('error' in b['pairs'].get(n,{}) for b in blocks):continue
                if phase==0 and nominal_pairs and n in nominal_pairs:
                    block['pairs'][n]=nominal_pairs[n]
                    continue
                reader.check_cancel()
                pct=phase*30+int(29*i/max(len(pairs),1))
                if pause and reader.pause_requested.is_set():
                    if moved:
                        backend.restore_frequency(rf_state)
                        moved=False
                    pause(pct)
                    if shift:
                        moved=True
                        backend.set_frequency(offset)
                label=retry_labels.get(n) if retry_labels else None
                detail=f' [{label}]' if label else ''
                progress(pct,f'RF {shift/1000:+g} kHz: {n} ({i+1}/{len(pairs)}){detail}')
                entry=dict(indices=[],readbacks=[])
                pair_mask=active.copy()
                for previous in blocks:
                    for idx in previous['pairs'][n]['indices']:pair_mask &= mask(idx)
                def coverage_lost(index,selected):
                    available=mask(index)
                    return (not (pair_mask & available & selected).any() or
                            (strict_loss and (active & (loss_selection() if loss_selection else selected) & ~available).any()))
                try:
                    for target in targets:
                        reader.check_cancel()
                        write(n,float(target))
                        actual=read_current(n)
                        idx=sample('probe',n,actual)
                        selected=selection()
                        if coverage_lost(idx,selected) and n in retry_names:
                            progress(pct,f'RF {shift/1000:+g} kHz: {n} at {actual:g} A, retry BPM in 3 s')
                            if reader.cancel.wait(3.):reader.check_cancel()
                            actual=read_current(n)
                            idx=sample('probe retry',n,actual)
                            selected=selection()
                        entry['indices'].append(idx)
                        entry['readbacks'].append(actual)
                        if coverage_lost(idx,selected):
                            reason=f'{n}: BPM coverage lost at {actual:g} A, RF {shift/1000:+g} kHz'
                            progress(pct,reason)
                            entry.update(error=reason,failed_current=float(target))
                            break
                        pair_mask &= mask(idx)
                except MagnetReadbackError as exc:
                    if set(exc.names)!={n}:raise
                    entry.update(error=str(exc),readback_error=True)
                finally:
                    restore(n,float(centers[n]))
                if 'error' in entry and not entry.get('readback_error'):
                    ref=reference('probe recovery',active,n,float(centers[n]))
                    if not (active & mask(ref) & selection()).any():
                        raise ValueError(f'{n}: BPM coverage unavailable at original current')
                block['pairs'][n]=entry
            block['end']=reference('reference end',active)
            common=active & mask(block['end'])
            if not (common & selection()).any():raise ValueError('No common enabled BPMs after RF block')
            delta=orbit_xy(frames[block['end']])-orbit_xy(frames[start])
            healthy=mask(start) & mask(block['end'])
            block['reference_drift_rms_mm']=np.sqrt(np.mean(delta[:,healthy]**2,axis=1))
            blocks.append(block)
            if transaction is not None:transaction.pop('partial',None)
            saved()
    finally:
        if moved:backend.restore_frequency(rf_state)
    reader.check_cancel()
    return common


def orbit_xy(frame):
    return np.asarray([frame['x'],frame['y']],float)


def momentum_span(sampling,backend):
    return -sampling.frequency_change_khz*1000/(backend.nominal_frequency_hz*sampling.momentum_compaction)


def reference_signal(frames,indices,sampling,backend):
    nominal,minus,plus=[orbit_xy(frames[i]) for i in indices]
    return np.vstack(((plus-minus)/momentum_span(sampling,backend),nominal))


def response_column(frames,blocks,n,sampling,backend):
    """Normalize each orbit difference by the span actually read at that RF."""
    slopes=[]
    valid=np.ones(len(backend.bpm_names),bool)
    for block in blocks:
        entry=block['pairs'][n]
        first,last=[frames[i] for i in entry['indices']]
        low,high=entry['readbacks']
        slopes.append((orbit_xy(last)-orbit_xy(first))/(high-low))
        valid &= np.asarray(first['valid'],bool) & np.asarray(last['valid'],bool)
    central,minus,plus=slopes
    return np.vstack(((plus-minus)/momentum_span(sampling,backend),central)),valid
