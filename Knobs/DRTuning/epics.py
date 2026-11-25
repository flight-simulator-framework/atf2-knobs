"""Additional live tuning capabilities. Import/constructor never writes PVs."""
import time
import hashlib
import numpy as np
from Knobs.DRCorrection.backends import EpicsBackend
from Knobs.DRCorrection.tbt import analyze_tbt
from .acquisition import TBTController,ReadbackTBTController,tbt_orbit_mode
from .calibration import coil_k1l
from .hardware import main_supply_mappings,quadrupole_supply
from . import power_pause


class LiveBackend(EpicsBackend):
    def __init__(self,profile,client=None):
        super().__init__(profile,client)
        self.monitor_klystrons = client is None
        self.quadrupoles={n:p for n,p in profile.get('quadrupoles',{}).items() if p.get('mapping_confirmed')}
        self.main_quadrupoles=main_supply_mappings(self.quadrupoles)
        self._calibration={}
    def get(self,pv):
        power_pause.checkpoint()
        if hasattr(self.client,'ca'):self.client.ca.use_initial_context()
        return super().get(pv)
    @power_pause.fresh_measurement()
    def read_xsr_sizes(self):
        from .emittance import read_xsr_sizes
        return read_xsr_sizes(self.get)
    stable_beam_read = staticmethod(power_pause.stable_read)

    def orbit_samples(self,count,cancel,interval_s=0.):
        from .fresh import updates
        p=self.profile['orbit']
        pvs=[p['packed_pv']] if p.get('packed_pv') else [p[k] for k in ('x','y','tmit')]
        for records in updates(self.metadata,pvs,count,cancel,
                timeout_s=self.profile.get('update_timeout_s',15.),interval_s=interval_s):
            result=self.orbit({pv:r['value'] for pv,r in records.items()})
            result.update(timestamps={pv:float(r['timestamp']) for pv,r in records.items()},
                          freshness_verified=True,same_shot_verified=False)
            yield result
    @power_pause.fresh_measurement()
    def sample_xsr(self,cancel,count=10,callback=lambda *_:None):
        from .fresh import updates,scalar_statistics
        pv='XSR:PROJ_V_SIGMA';values=[];stamps=[]
        for records in updates(self.metadata,[pv],count,cancel,timeout_s=self.profile.get('update_timeout_s',15.)):
            values.append(float(records[pv]['value']));stamps.append(float(records[pv]['timestamp']))
            result=scalar_statistics(values,stamps,pv);callback(result)
        return result
    def put(self,pv,value):
        power_pause.checkpoint()
        if hasattr(self.client,'ca'):self.client.ca.use_initial_context()
        return super().put(pv,value)
    @power_pause.restoration
    def restore_probe(self,kind,name,value):
        return super().restore_probe(kind,name,value)

    @power_pause.restoration
    def restore_frequency(self,state):
        return super().restore_frequency(state)

    def quadrupole_current(self,name):
        return self.magnet_readback(name,quadrupole_supply(self,name)['read'])
    def quadrupole_k1l(self,name,current):
        p=quadrupole_supply(self,name);cal=p['calibration']
        if not cal.get('confirmed'):raise ValueError('Confirm current-to-K1L calibration for '+name)
        if cal['kind']=='polynomial':
            value=float(np.polynomial.polynomial.polyval(current,cal['coefficients']))
        elif cal['kind']=='mag_ki_main':
            from machine_interfaces_atf2.MagKi import MagKiWrapper
            library=cal.get('library')
            if library is None:
                from .calibration import build_library
                library=str(build_library())
            if library not in self._calibration:self._calibration[library]=MagKiWrapper(library)
            trim=cal.get('coil','main')=='trim'
            main=float(self.get(cal['main_current_pv'])) if trim else current
            value=coil_k1l(self._calibration[library],cal['magnet'],cal['energy_GeV'],main,current if trim else 0.,'trim' if trim else 'main')
        else:raise ValueError('Unknown quadrupole calibration')
        if not np.isfinite(value):raise ValueError('Nonfinite calibrated K1L')
        return value
    def quadrupole_field_check(self,name,current):
        p=quadrupole_supply(self,name).get('field_check')
        if not p or not p.get('read_pv'):return None
        factor=float(p['k1l_per_unit']);tolerance=float(p['tolerance_per_m'])
        if not np.isfinite([factor,tolerance]).all() or factor==0 or tolerance<0:raise ValueError('Configure field-PV unit conversion and tolerance')
        if p.get('process_pv'):self.put(p['process_pv'],1)
        raw=float(self.get(p['read_pv']));measured=raw*factor;calibrated=self.quadrupole_k1l(name,current)
        if not np.isfinite(measured):raise ValueError('Invalid field PV')
        # Profile must identify the same coil quantity (main or trim) on both sides.
        return dict(pv=p['read_pv'],raw=raw,k1l_per_unit=factor,pv_k1l=measured,
                    calibrated_k1l=calibrated,within_tolerance=abs(measured-calibrated)<=tolerance)
    def validate_quadrupole_targets(self,name,values):
        p=quadrupole_supply(self,name)
        if not p.get('mapping_confirmed'):raise ValueError('Confirm quadrupole PV/coil mapping for '+name)
        delay=float(p['settle_s'])
        if not np.isfinite(delay) or delay<0:raise ValueError('Invalid quadrupole settling time')
        lo=float(self.get(p['lower_limit_pv']));hi=float(self.get(p['upper_limit_pv']))
        v=np.asarray(values,float)
        if not np.isfinite([lo,hi]).all() or lo>=hi or not np.isfinite(v).all() or np.any(v<lo) or np.any(v>hi):
            raise ValueError('Quadrupole outside supply current limits')
        for value in v:self.quadrupole_k1l(name,float(value))
    def set_quadrupole_current(self,name,value):
        self.validate_quadrupole_targets(name,[value]);p=quadrupole_supply(self,name)
        original=self.quadrupole_current(name)
        def wait(target):
            deadline=time.monotonic()+self.profile['readback_timeout_s']
            while abs(self.quadrupole_current(name)-target)>self.profile['current_tolerance_A']:
                if time.monotonic()>=deadline:raise TimeoutError('Quadrupole readback did not settle')
                time.sleep(.05)
        try:
            self.put(p['set'],float(value));wait(value)
            delay=float(p['settle_s'])
            if not np.isfinite(delay) or delay<0:raise ValueError('Invalid quadrupole settling time')
            time.sleep(delay)
        except Exception:
            self.put(p['set'],original);wait(original);raise
    @power_pause.restoration
    def restore_quadrupole_current(self,name,value):
        """Known original current can be commanded even after a readback failure."""
        self.validate_quadrupole_targets(name,[value]);p=quadrupole_supply(self,name)
        self.put(p['set'],float(value));deadline=time.monotonic()+self.profile['readback_timeout_s']
        while True:
            reading=self.quadrupole_current(name)
            if np.isfinite(reading) and abs(reading-value)<=self.profile['current_tolerance_A']:break
            if time.monotonic()>=deadline:raise TimeoutError('QM restoration readback did not settle: '+name)
            time.sleep(.05)
        time.sleep(float(p['settle_s']))
    @power_pause.fresh_measurement(retry_errors=())
    def tbt_acquire(self,turns=1024,start_turn=1,cancel=None,progress=lambda *_:None):
        self.validate_tbt_acquisition()
        progress(0,'New shot: checking acquisition mode and waveform updates')
        a=self.profile['tbt']['acquisition']
        with tbt_orbit_mode(self.get,self.put,a,cancel=cancel,progress=progress) as mode:
            if a.get('mode')=='readback_checks':
                controller=ReadbackTBTController(self.get,self.put,lambda:self.metadata(a['event_pv'])['timestamp'],a)
                result=controller.acquire(lambda:self.tbt_snapshot(turns,cancel=cancel,progress=progress),start_turn=start_turn,cancel=cancel)
            else:
                controller=TBTController(self.get,self.put,a)
                result=controller.acquire(lambda:self.read_tbt(turns,cancel=cancel,progress=progress),start_turn=start_turn,cancel=cancel)
        result['orbit_mode_control']=mode
        return result
    @power_pause.fresh_measurement()
    def read_tbt(self,turns=1024,*,cancel=None,progress=lambda *_:None):
        """Read existing waveforms only; no trigger, PROC or machine write."""
        p=self.profile.get('tbt',{})
        if not p.get('mapping_confirmed'):raise ValueError('Confirm TBT channel-to-BPM mapping first')
        wave,evidence=self.tbt_snapshot(turns,cancel=cancel,progress=progress)
        wave.update(fresh_acquisition=False,same_shot_verified=False,waveform_evidence=evidence)
        return wave
    def metadata(self,pv):
        power_pause.checkpoint()
        if hasattr(self.client,'ca'):self.client.ca.use_initial_context()
        result=self.client.get_pv(pv,form='time',connect=True).get_with_metadata(use_monitor=False,form='time',timeout=3.)
        if result is None or result.get('value') is None or not np.isfinite(result.get('timestamp',np.nan)):
            raise RuntimeError('PV timestamp/data unavailable: '+pv)
        if result.get('severity',0)>=2:raise RuntimeError('PV alarm: '+pv)
        return result
    def tbt_snapshot(self,turns,*,cancel=None,progress=lambda *_:None):
        p=self.profile['tbt'];names=p.get('names') or self.bpm_names;arrays={};evidence={}
        total=sum(len(p[a]) if p.get('layout')=='per_bpm' else 1 for a in ('x','y','tmit'));count=0
        for axis in ('x','y','tmit'):
            pvs=p[axis] if p.get('layout')=='per_bpm' else [p[axis]];columns=[]
            for pv in pvs:
                if cancel is not None and cancel.is_set():raise RuntimeError('TBT read cancelled')
                progress(round(100*count/total),f'{pv} ({count+1}/{total})')
                try:sample=self.metadata(pv);v=np.asarray(sample['value'],float)
                except Exception as exc:raise RuntimeError(f'{pv}: {exc}') from exc
                if p.get('layout')=='per_bpm' and (v.ndim!=1 or len(v)<turns):
                    raise ValueError(f'{pv}: expected at least {turns} samples; received shape {v.shape}')
                evidence[pv]=dict(timestamp=float(sample['timestamp']),sha256=hashlib.sha256(v.tobytes()).hexdigest())
                columns.append(v);count+=1
            v=np.column_stack(columns) if p.get('layout')=='per_bpm' else columns[0].reshape(p['shape'])
            if p.get('layout')!='per_bpm' and p.get('axes')=='bpm,turn':v=v.T
            if v.shape[0]<turns or v.shape[1]!=len(names):raise ValueError('TBT shape mismatch')
            arrays[axis]=v[:turns]
        scale=float(p['mm_per_unit'])
        if not np.isfinite(scale) or scale<=0:raise ValueError('Invalid TBT units')
        result=dict(names=names,turns=list(range(1,turns+1)),x=arrays['x']*scale,y=arrays['y']*scale,
            tmit=arrays['tmit'],valid=np.isfinite(arrays['x'])&np.isfinite(arrays['y'])&(arrays['tmit']>0),
            position_unit='mm',bpm_noise_rms_mm=p['noise_mm'],orbit_regime='live waveform',equilibrium_validated=False)
        progress(100,f'{len(names)} BPM × {turns} turns read complete')
        return result,evidence
    def validate_tune_measurement(self):
        tunes=self.profile.get('qmod_tunes',{})
        if tunes.get('mode')=='pvs':
            for key in ('x','y','timeout_s','max_timestamp_skew_s'):
                if tunes.get(key) is None:raise ValueError('Missing tune readback '+key)
            timing=np.array([tunes['timeout_s'],tunes['max_timestamp_skew_s']],float)
            if not np.isfinite(timing).all() or (timing<=0).any():raise ValueError('Invalid tune timing limits')
            return
        self.validate_tbt_acquisition()
    def validate_tbt_acquisition(self):
        p=self.profile.get('tbt',{});a=p.get('acquisition',{})
        if not p.get('mapping_confirmed'):raise ValueError('Confirm TBT channel mapping before a tune scan')
        if a.get('mode')=='readback_checks':
            if not a.get('event_pv'):raise ValueError('Configure the legacy TBT event PV')
        elif not a.get('completion_confirmed') or not a.get('generation_pv'):
            raise ValueError('Configure TBT completion or select readback_checks mode')
        for key in ('mode_pv','shot_mode_pv','turn_pv','snap_pv','timeout_s'):
            if not a.get(key):raise ValueError('Missing TBT acquisition '+key)
    def snapshot(self,readback_errors=None,excluded=()):
        result=super().snapshot(readback_errors=readback_errors,excluded=excluded)
        result['quadrupoles']=self.snapshot_magnets(self.quadrupoles,self.quadrupole_current,readback_errors,excluded)
        return result
    def same_settings(self,left,right):
        a=left.get('quadrupoles',{});b=right.get('quadrupoles',{})
        return (super().same_settings(left,right) and set(a)==set(b) and
                all(abs(a[n]-b[n])<=self.profile['current_tolerance_A'] for n in a))
    @power_pause.restoration
    def restore(self,snapshot):
        super().restore(snapshot)
        for n,value in snapshot.get('quadrupoles',{}).items():
            if abs(self.quadrupole_current(n)-value)>self.profile['current_tolerance_A']:self.set_quadrupole_current(n,value)
    def measure_tunes(self,cancel=None):
        p=self.profile.get('qmod_tunes',{})
        if p.get('mode')=='pvs':
            return self._measure_tune_pvs(p,cancel)
        wave=self.tbt_acquire(1024,cancel=cancel)
        fit=analyze_tbt(wave,start_turn=wave['turns'][0])
        if not all(fit['planes'][a].get('valid') for a in ('x','y')):
            raise RuntimeError('Fresh TBT must contain measurable H and V excitation for tune scan')
        return dict(tunes=[fit['planes'][a]['tune'] for a in ('x','y')],source='TBT',
                    acquisition_generation=wave.get('acquisition_generation'),
                    same_shot_verified=wave.get('same_shot_verified',wave.get('fresh_acquisition',False)),
                    acquisition_quality=wave.get('acquisition_quality','completion generation'),
                    waveform_evidence=wave.get('waveform_evidence'))

    @power_pause.fresh_measurement()
    def _measure_tune_pvs(self,p,cancel):
        self.validate_tune_measurement()
        if cancel is not None and cancel.is_set():raise RuntimeError('Tune acquisition cancelled')
        initial=[self.metadata(p[a]) for a in ('x','y')];deadline=time.monotonic()+float(p['timeout_s'])
        while True:
            if cancel is not None and cancel.is_set():raise RuntimeError('Tune acquisition cancelled')
            readings=[self.metadata(p[a]) for a in ('x','y')]
            stamps=[r['timestamp'] for r in readings];values=[float(r['value']) for r in readings]
            updated=[stamps[i]>initial[i]['timestamp'] for i in range(2)]
            valid=[bool(np.isfinite(v) and 0<v<1) for v in values]
            skew=abs(stamps[0]-stamps[1])
            if all(updated) and skew<=p['max_timestamp_skew_s'] and all(valid):
                return dict(tunes=values,source='existing TuneDR PVs',pvs=[p['x'],p['y']],timestamps=stamps,
                    initial_timestamps=[r['timestamp'] for r in initial],timestamp_skew_s=skew,
                    same_shot_verified=False,readback_update_verified=True,
                    acquisition_quality='both tune readbacks updated after request; common shot ID unavailable')
            if time.monotonic()>deadline:
                details='; '.join(f'{axis} {p[key]}: value={values[i]:.9g}, updated={updated[i]}, '
                    f'valid_0_to_1={valid[i]}, timestamp={stamps[i]:.6f}, initial={initial[i]["timestamp"]:.6f}'
                    for i,(axis,key) in enumerate((('H','x'),('V','y'))))
                raise TimeoutError(f'No fresh H/V tune pair after {p["timeout_s"]:g} s; {details}; '
                    f'H/V timestamp skew={skew:.6g} s (limit {p["max_timestamp_skew_s"]:g} s). '
                    'Check TuneDR acquisition/reader and H/V FFT peak results; BPM COD updates alone do not update tune PVs.')
            if cancel is None:time.sleep(.02)
            elif cancel.wait(.02):raise RuntimeError('Tune acquisition cancelled')
