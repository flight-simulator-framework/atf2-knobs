"""Turn1024 start/restore sequence with explicit completion evidence.

Legacy TBT:TUNE:H is a tune update, not proof all 96 BPM buffers are ready.
The deployment profile must identify a completion generation after ALL data is
ready. No elapsed delay is accepted as evidence of a completed acquisition.
"""
import time
from contextlib import contextmanager
import numpy as np


@contextmanager
def tbt_orbit_mode(get,put,config,*,cancel=None,progress=lambda *_:None):
    """Temporarily select TBT (0), verify readback, then restore the original mode.

    This is separate from crate MODE and drbpm:shot:mode. Restoration deliberately
    ignores cancellation, and a failed write is treated as possibly applied.
    """
    pv=config.get('orbit_mode_pv','DRBPM:ORBIT_MODE')
    timeout=float(config.get('orbit_mode_timeout_s',5.))
    settle=float(config.get('orbit_mode_settle_s',.5))
    if not pv or not np.isfinite([timeout,settle]).all() or timeout<=0 or settle<0:
        raise ValueError('Invalid TBT ORBIT_MODE timing / PV configuration')
    def check_cancel():
        if cancel is not None and cancel.is_set():raise RuntimeError('TBT cancelled')
    def read():
        value=get(pv)
        if not np.isscalar(value) or value not in (0,1,2,3):
            raise ValueError(f'{pv}: invalid mode readback {value!r}')
        return int(value)
    def wait(target,restoring=False):
        deadline=time.monotonic()+timeout
        while True:
            if not restoring:check_cancel()
            actual=read()
            if actual==target:break
            if time.monotonic()>=deadline:
                raise TimeoutError(f'{pv}: expected {target}, read {actual}')
            time.sleep(.02)
        until=time.monotonic()+settle
        while time.monotonic()<until:
            if not restoring:check_cancel()
            time.sleep(min(.02,max(0.,until-time.monotonic())))
        if read()!=target:raise RuntimeError(f'{pv}: mode changed during settling; expected {target}')
    check_cancel();original=read();touched=False
    evidence=dict(pv=pv,original=original,acquisition=0,restored=False)
    try:
        progress(0,f'{pv}: selecting TBT (0), original mode {original}')
        if original!=0:
            touched=True;put(pv,0)
        wait(0)
        yield evidence
        if read()!=0:raise RuntimeError(f'{pv}: changed during TBT acquisition')
    finally:
        if touched:
            progress(100,f'{pv}: restoring mode {original}')
            try:put(pv,original);wait(original,restoring=True)
            except Exception as exc:raise RuntimeError(f'ORBIT_MODE restoration failed: {pv} -> {original}: {exc}') from exc
        elif read()!=original:
            raise RuntimeError(f'{pv}: original mode {original} is no longer active')
        evidence['restored']=True


class TBTController:
    def __init__(self,get,put,config):self.get=get;self.put=put;self.config=config
    def acquire(self,reader,*,start_turn=1,cancel=None):
        p=self.config
        if not p.get('completion_confirmed') or not p.get('generation_pv'):
            raise ValueError('Configure/confirm TBT all-waveforms completion generation PV first')
        if not isinstance(start_turn,int) or start_turn<1:raise ValueError('Invalid start turn')
        timeout=float(p['timeout_s']);settle=float(p.get('arm_settle_s',1.))
        if not np.isfinite([timeout,settle]).all() or timeout<=0 or settle<0:raise ValueError('Invalid acquisition timing')
        controls=[(p['mode_pv'],p.get('one_shot_value','One-shot')),
            (p['shot_mode_pv'],1),(p['turn_pv'],start_turn)]
        originals=[(pv,self.get(pv)) for pv,_ in controls]
        touched=[]
        try:
            for (pv,value),(_,old) in zip(controls,originals):
                touched.append((pv,old));self.put(pv,value)
            deadline=time.monotonic()+settle
            while time.monotonic()<deadline:
                if cancel is not None and cancel.is_set():raise RuntimeError('TBT cancelled')
                time.sleep(min(.02,max(0,deadline-time.monotonic())))
            before=self.get(p['generation_pv'])
            if not np.isscalar(before) or not np.isfinite(before):raise ValueError('Invalid TBT completion generation')
            self.put(p['snap_pv'],1);deadline=time.monotonic()+timeout
            while True:
                if cancel is not None and cancel.is_set():raise RuntimeError('TBT cancelled')
                generation=self.get(p['generation_pv'])
                if not np.isscalar(generation) or not np.isfinite(generation):raise ValueError('Invalid TBT completion generation')
                if generation!=before:break
                if time.monotonic()>=deadline:raise TimeoutError('No new completed TBT acquisition')
                time.sleep(.02)
            for pv in p.get('process_pvs',[]):self.put(pv,1)
            result=reader()
            if self.get(p['generation_pv'])!=generation:raise RuntimeError('TBT generation changed during waveform read')
            result.update(acquisition_generation=generation,fresh_acquisition=True,
                turns=list(range(start_turn,start_turn+len(result['turns']))))
            return result
        finally:
            errors=[]
            for pv,value in reversed(touched):
                try:self.put(pv,value)
                except Exception as error:errors.append(f'{pv}: {error}')
            if errors:raise RuntimeError('TBT mode restoration failed: '+'; '.join(errors))


class ReadbackTBTController:
    """Legacy event + per-waveform timestamps + stable repeated data checks.

    This detects stale/torn reads but CANNOT prove a common hardware shot ID.
    The quality flag deliberately remains unverified in every saved result.
    """
    def __init__(self,get,put,event_timestamp,config):
        self.get=get;self.put=put;self.event_timestamp=event_timestamp;self.config=config
    def acquire(self,snapshot,*,start_turn=1,cancel=None):
        p=self.config;timeout=float(p['timeout_s']);settle=float(p.get('readout_settle_s',1.))
        if not np.isfinite([timeout,settle]).all() or timeout<=0 or settle<0:raise ValueError('Invalid acquisition timing')
        if not isinstance(start_turn,int) or start_turn<1:raise ValueError('Invalid first turn')
        controls=[(p['mode_pv'],p.get('one_shot_value','One-shot')),(p['shot_mode_pv'],1),(p['turn_pv'],start_turn)]
        originals=[(pv,self.get(pv)) for pv,_ in controls];touched=[]
        previous=snapshot()[1]
        def check():
            if cancel is not None and cancel.is_set():raise RuntimeError('TBT cancelled')
        def check_controls():
            for pv,expected in ((p['turn_pv'],start_turn),(p['shot_mode_pv'],1)):
                actual=self.get(pv)
                if actual!=expected:
                    raise RuntimeError(f'TBT control changed: {pv}, expected {expected}, read {actual}; check other acquisition clients')
        def pause(seconds):
            until=time.monotonic()+seconds
            while time.monotonic()<until:check();time.sleep(min(.02,max(0,until-time.monotonic())))
        try:
            for (pv,value),(_,old) in zip(controls,originals):touched.append((pv,old));self.put(pv,value)
            pause(float(p.get('arm_settle_s',1.)))
            check_controls()
            event0=self.event_timestamp();self.put(p['snap_pv'],1);deadline=time.monotonic()+timeout
            while True:
                check();event=self.event_timestamp()
                if event>event0:break
                if time.monotonic()>deadline:raise TimeoutError('No post-SNAP TBT tune-update event')
                pause(.02)
            pause(settle)
            for pv in p.get('process_pvs',[]):check();self.put(pv,1)
            while True:
                check();check_controls();start_event=self.event_timestamp()
                data,first=snapshot();pause(float(p.get('stable_read_interval_s',.05)));_,second=snapshot()
                end_event=self.event_timestamp();check_controls()
                updated=(set(first)==set(previous) and all(first[n]['timestamp']>previous[n]['timestamp'] for n in first))
                # A record timestamp may advance without a new waveform. Compare
                # the payload for stability, while still rejecting timestamp
                # regression and requiring post-baseline update evidence.
                unstable=sorted(n for n in set(first)|set(second)
                    if n not in first or n not in second or first[n]['sha256']!=second[n]['sha256'])
                regressed=sorted(n for n in set(first)&set(second) if second[n]['timestamp']<first[n]['timestamp'])
                timestamp_only=sorted(n for n in set(first)&set(second)
                    if first[n]['sha256']==second[n]['sha256'] and second[n]['timestamp']>first[n]['timestamp'])
                stable=(set(first)==set(second) and not unstable and not regressed)
                changed=any(first[n]['sha256']!=previous[n]['sha256'] for n in set(first)&set(previous))
                if updated and stable and changed and start_event==end_event:
                    data.update(turns=list(range(start_turn,start_turn+len(data['turns']))),fresh_acquisition=False,
                        readback_update_verified=True,same_shot_verified=False,
                        acquisition_quality='legacy event + updated timestamps + stable waveform content; common shot ID unavailable',
                        event_timestamp=end_event,waveform_evidence=first,repeat_waveform_evidence=second,
                        timestamp_only_updates=timestamp_only)
                    return data
                if time.monotonic()>deadline:
                    stale=sorted(n for n in previous if n not in first or first[n]['timestamp']<=previous[n]['timestamp'])
                    unexpected=sorted(set(first)-set(previous))
                    def describe(names):return f'{len(names)} ['+', '.join(names[:8])+(', ...' if len(names)>8 else '')+']'
                    reason=('No waveform content changed since before SNAP. New shot NOT verified. '
                            'Compare with acquisition in the existing Turn1024 tool and inspect the TBT trigger/reader.'
                            if not changed else 'Waveform content or acquisition event did not remain stable.')
                    raise TimeoutError('TBT waveform update/stability checks failed; '
                        f'stale/missing PVs={describe(stale)}; unstable PVs={describe(unstable)}; '
                        f'timestamp-only updates={describe(timestamp_only)}; timestamp regressions={describe(regressed)}; '
                        f'unexpected PVs={describe(unexpected)}; content_changed={changed}; '
                        f'tune_event_changed_during_read={start_event!=end_event}; {reason}')
                pause(.02)
        finally:
            errors=[]
            for pv,value in reversed(touched):
                try:self.put(pv,value)
                except Exception as error:errors.append(f'{pv}: {error}')
            if errors:raise RuntimeError('TBT mode restoration failed: '+'; '.join(errors))
