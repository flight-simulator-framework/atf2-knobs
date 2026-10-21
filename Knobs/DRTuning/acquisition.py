"""Turn1024 start/restore sequence with explicit completion evidence.

Legacy TBT:TUNE:H is a tune update, not proof all 96 BPM buffers are ready.
The deployment profile must identify a completion generation after ALL data is
ready. No elapsed delay is accepted as evidence of a completed acquisition.
"""
import time
import numpy as np


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
        def pause(seconds):
            until=time.monotonic()+seconds
            while time.monotonic()<until:check();time.sleep(min(.02,max(0,until-time.monotonic())))
        try:
            for (pv,value),(_,old) in zip(controls,originals):touched.append((pv,old));self.put(pv,value)
            pause(float(p.get('arm_settle_s',1.)))
            event0=self.event_timestamp();self.put(p['snap_pv'],1);deadline=time.monotonic()+timeout
            while True:
                check();event=self.event_timestamp()
                if event>event0:break
                if time.monotonic()>deadline:raise TimeoutError('No post-SNAP TBT tune-update event')
                pause(.02)
            pause(settle)
            for pv in p.get('process_pvs',[]):check();self.put(pv,1)
            while True:
                check();start_event=self.event_timestamp()
                data,first=snapshot();pause(float(p.get('stable_read_interval_s',.05)));_,second=snapshot()
                end_event=self.event_timestamp()
                updated=(set(first)==set(previous) and all(first[n]['timestamp']>previous[n]['timestamp'] for n in first))
                stable=(first==second)
                changed=any(first[n]['sha256']!=previous[n]['sha256'] for n in set(first)&set(previous))
                if updated and stable and changed and start_event==end_event:
                    data.update(turns=list(range(start_turn,start_turn+len(data['turns']))),fresh_acquisition=False,
                        readback_update_verified=True,same_shot_verified=False,
                        acquisition_quality='legacy event + updated stable waveforms; common shot ID unavailable',
                        event_timestamp=end_event,waveform_evidence=first)
                    return data
                if time.monotonic()>deadline:raise TimeoutError('TBT waveform update/stability checks failed')
                pause(.02)
        finally:
            errors=[]
            for pv,value in reversed(touched):
                try:self.put(pv,value)
                except Exception as error:errors.append(f'{pv}: {error}')
            if errors:raise RuntimeError('TBT mode restoration failed: '+'; '.join(errors))
