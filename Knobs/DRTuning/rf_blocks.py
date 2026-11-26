"""Measure dispersion responses with a shared RF setting for all current probes."""
import numpy as np

from Knobs.DRCorrection.probe_quality import mean_variance
from Knobs.DRCorrection.backends import MagnetReadbackError


class ScanFrequency:
    """Own RF restoration across one scan, including transitions that fail partway."""

    def __init__(self,backend):
        self.backend=backend
        self.origin=backend.frequency()
        self.original_state=backend.frequency_state()
        self.target=self.origin
        self.state=self.original_state
        self.moved=False

    def move(self,target):
        if target==self.target:return
        if target==self.origin:
            self.restore()
            return
        self.moved=True
        self.backend.set_frequency(target)
        self.target=target
        if isinstance(self.original_state,dict):self.state=self.backend.frequency_state()

    def restore(self):
        if self.moved:
            self.backend.restore_frequency(self.original_state)
            self.target=self.origin
            self.state=self.original_state
            self.moved=False

    def retry_order(self,all_cached):
        side=1 if self.target>self.origin else -1
        return [0,side,-side] if all_cached else [side,0,-side]

    def expected_settings(self,baseline):
        expected=dict(baseline,frequency=self.target)
        if 'ramp_state' in expected:expected['ramp_state']=self.state
        return expected


def matrix_activity(reader, **fields):
    observer = getattr(reader, 'matrix_activity', None)
    if observer is not None:
        observer(fields)


def measure_blocks(reader, sampling, pairs, centers, blocks, *, write, read, restore,
                   selection, strict_loss, retry_names, progress, saved=lambda:None,
                   pause=None, baseline_ready=None, nominal_ready=None, loss_selection=None,
                   transaction=None, nominal_pairs=None, retry_labels=None, scan_frequency=None, rf_order=(0,-1,1)):
    """Track restored QM pairs within the current RF block for resume. Frame indices refer to the raw orbit log."""
    backend=reader.backend
    rf=scan_frequency or ScanFrequency(backend)
    origin=rf.origin
    half=sampling.frequency_change_khz*500
    if not np.isfinite([half,sampling.momentum_compaction]).all() or min(half,sampling.momentum_compaction)<=0:
        raise ValueError('Positive RF span and momentum compaction required')
    backend.validate_frequency_targets([origin-half,origin+half])
    frames=reader.orbit_measurements

    observer = getattr(reader, 'matrix_activity', None)

    def report(stage, shots=None):
        if observer is not None:
            observer(dict(point=point, stage=stage, shots=shots))

    def sample(label, magnet=None, current=None, stage=None):
        if observer is None:
            reader._orbit(sampling)
        else:
            stage = stage or label.capitalize()
            def on_sample(count, total):
                report(stage, (count, total))
            on_sample(0, sampling.bpm_samples)
            reader._orbit(sampling, on_sample=on_sample)
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
        selected=selection()
        enabled=loss_selection() if loss_selection else selected
        if (required & enabled & ~mask(idx)).any():
            report(f'BPM validity loss · retry {label} in 3 s')
            progress(phase*30,f'RF {shift/1000:+g} kHz: retry {label} in 3 s')
            if reader.cancel.wait(3.):reader.check_cancel()
            idx=sample(label,magnet,current)
        return idx

    common=np.ones(len(backend.bpm_names),bool)
    for block in blocks:
        common &= mask(block['start']) & mask(block['end'])
    try:
        for phase,sign in enumerate(rf_order):
            if phase<len(blocks):continue
            reader.check_cancel()
            shift=sign*half
            offset=origin+shift
            rf_point = f'RF {shift/1000:+g} kHz · block {phase+1}/{len(rf_order)}'
            point = rf_point
            report('Setting RF')
            rf.move(offset)
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
            if sign==0 and nominal_ready:nominal_ready(orbit_xy(frames[start]),active.copy())
            if phase==2 and baseline_ready:
                baseline_ready(reference_signal(frames,[b['start'] for b in blocks]+[start],sampling,backend),active.copy())
            if transaction is not None:transaction['partial']=block
            for i,(n,targets) in enumerate(pairs.items()):
                if n in block['pairs'] or any('error' in b['pairs'].get(n,{}) for b in blocks):continue
                if sign==0 and nominal_pairs and n in nominal_pairs:
                    block['pairs'][n]=nominal_pairs[n]
                    continue
                reader.check_cancel()
                pct=phase*30+int(29*i/max(len(pairs),1))
                label=retry_labels.get(n) if retry_labels else None
                point=f'{rf_point} · {n} ({i+1}/{len(pairs)})'
                if label:point+=f' · {label}'
                if pause and reader.pause_requested.is_set():
                    report('Pausing · returning RF to nominal')
                    rf.restore()
                    report('Paused')
                    pause(pct)
                    report('Resuming · setting RF')
                    rf.move(offset)
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
                    for side,target in zip(('Low','High'),targets):
                        reader.check_cancel()
                        command=f'{side} · set {target:g} A'
                        report(command)
                        write(n,float(target))
                        actual=read_current(n)
                        idx=sample('probe',n,actual,f'{command} · read {actual:g} A')
                        selected=selection()
                        missing=(pair_mask & selected & ~mask(idx)).any()
                        if n in retry_names and (missing or coverage_lost(idx,selected)):
                            report(f'{command} · BPM validity loss · retry in 3 s')
                            progress(pct,f'RF {shift/1000:+g} kHz: {n} at {actual:g} A, retry BPM in 3 s')
                            if reader.cancel.wait(3.):reader.check_cancel()
                            actual=read_current(n)
                            idx=sample('probe retry',n,actual,f'{command} · read {actual:g} A · BPM retry')
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
                    try:
                        report(f'Restoring {n} to {centers[n]:g} A')
                    finally:
                        restore(n,float(centers[n]))
                if 'error' in entry and not entry.get('readback_error'):
                    ref=reference('probe recovery',active,n,float(centers[n]))
                    if not (active & mask(ref) & selection()).any():
                        raise ValueError(f'{n}: BPM coverage unavailable at original current')
                block['pairs'][n]=entry
            point=rf_point
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
        if scan_frequency is None:rf.restore()
    reader.check_cancel()
    return common


def orbit_xy(frame):
    return np.asarray([frame['x'],frame['y']],float)


def momentum_span(sampling,backend):
    return -sampling.frequency_change_khz*1000/(backend.nominal_frequency_hz*sampling.momentum_compaction)


def reference_signal(frames,indices,sampling,backend):
    nominal,minus,plus=[orbit_xy(frames[i]) for i in indices]
    return np.vstack(((plus-minus)/momentum_span(sampling,backend),nominal))


def orbit_slope(first,last,readbacks):
    """Orbit change per ampere, using the achieved low and high currents."""
    low,high=readbacks
    if not np.isfinite([low,high]).all() or high<=low:raise ValueError('Unresolved current span')
    return (last-first)/(high-low)


def canonical_blocks(blocks,order):
    return [blocks[order.index(sign)] for sign in (0,-1,1)]


def nominal_probe_resolved(frames,pair,mask,target_snr,axes):
    low,high=(frames[i] for i in pair['indices'])
    if not mask.any():return False
    signal2=0.
    noise2=0.
    for axis in axes:
        change=np.asarray(high[axis],float)[mask]-np.asarray(low[axis],float)[mask]
        signal2+=np.mean(change**2)
        noise2+=np.mean((mean_variance(low,axis)+mean_variance(high,axis))[mask])
    return bool(np.isfinite([signal2,noise2]).all() and signal2>1.e-24 and signal2>=target_snr**2*noise2)


def probe_noise(frames,blocks,n,sampling,backend,axis):
    variances=[]
    for block in blocks[1:]:
        entry=block['pairs'][n]
        endpoints=[mean_variance(frames[idx],axis) for idx in entry['indices']]
        span=np.diff(entry['readbacks'])[0]
        variances.append((endpoints[0]+endpoints[1])/span**2)
    return np.sqrt(variances[0]+variances[1])/abs(momentum_span(sampling,backend))


def response_column(frames,blocks,n,sampling,backend):
    """Normalize each orbit difference by the span actually read at that RF."""
    slopes=[]
    valid=np.ones(len(backend.bpm_names),bool)
    for block in blocks:
        entry=block['pairs'][n]
        first,last=[frames[i] for i in entry['indices']]
        slopes.append(orbit_slope(orbit_xy(first),orbit_xy(last),entry['readbacks']))
        valid &= np.asarray(first['valid'],bool) & np.asarray(last['valid'],bool)
    central,minus,plus=slopes
    return np.vstack(((plus-minus)/momentum_span(sampling,backend),central)),valid
