"""Additional live tuning capabilities. Import/constructor never writes PVs."""
import time
import hashlib
import numpy as np
from Knobs.DRCorrection.backends import EpicsBackend
from Knobs.DRCorrection.tbt import analyze_tbt
from .acquisition import TBTController,ReadbackTBTController
from .calibration import coil_k1l


class LiveBackend(EpicsBackend):
    def __init__(self,profile,client=None):
        super().__init__(profile,client)
        self.quadrupoles={n:p for n,p in profile.get('quadrupoles',{}).items() if p.get('mapping_confirmed')}
        self._calibration={}
    def get(self,pv):
        if hasattr(self.client,'ca'):self.client.ca.use_initial_context()
        return super().get(pv)
    def put(self,pv,value):
        if hasattr(self.client,'ca'):self.client.ca.use_initial_context()
        return super().put(pv,value)
    def quadrupole_current(self,name):
        value=float(self.get(self.quadrupoles[name]['read']))
        if not np.isfinite(value):raise RuntimeError('Invalid quadrupole readback')
        return value
    def quadrupole_k1l(self,name,current):
        p=self.quadrupoles[name];cal=p['calibration']
        if not cal.get('confirmed'):raise ValueError('Confirm current-to-K1L calibration for '+name)
        if cal['kind']=='polynomial':
            value=float(np.polynomial.polynomial.polyval(current,cal['coefficients']))
        elif cal['kind']=='mag_ki_main':
            from machine_interfaces_atf2.MagKi import MagKiWrapper
            library=cal.get('library')
            if library is None:
                import sys
                from pathlib import Path
                sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'simulation-agent'))
                from observable.calibration import build_library
                library=str(build_library())
            if library not in self._calibration:self._calibration[library]=MagKiWrapper(library)
            trim=cal.get('coil','main')=='trim'
            main=float(self.get(cal['main_current_pv'])) if trim else current
            value=coil_k1l(self._calibration[library],cal['magnet'],cal['energy_GeV'],main,current if trim else 0.,'trim' if trim else 'main')
        else:raise ValueError('Unknown quadrupole calibration')
        if not np.isfinite(value):raise ValueError('Nonfinite calibrated K1L')
        return value
    def quadrupole_field_check(self,name,current):
        p=self.quadrupoles[name].get('field_check')
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
        p=self.quadrupoles[name]
        if not p.get('mapping_confirmed'):raise ValueError('Confirm quadrupole PV/coil mapping for '+name)
        delay=float(p['settle_s'])
        if not np.isfinite(delay) or delay<0:raise ValueError('Invalid quadrupole settling time')
        lo=float(self.get(p['lower_limit_pv']));hi=float(self.get(p['upper_limit_pv']))
        v=np.asarray(values,float)
        if not np.isfinite([lo,hi]).all() or lo>=hi or not np.isfinite(v).all() or np.any(v<lo) or np.any(v>hi):
            raise ValueError('Quadrupole outside supply current limits')
        for value in v:self.quadrupole_k1l(name,float(value))
    def set_quadrupole_current(self,name,value):
        self.validate_quadrupole_targets(name,[value]);p=self.quadrupoles[name]
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
    def tbt_acquire(self,turns=1024,start_turn=1,cancel=None):
        self.validate_tbt_acquisition()
        a=self.profile['tbt']['acquisition']
        if a.get('mode')=='readback_checks':
            controller=ReadbackTBTController(self.get,self.put,lambda:self.metadata(a['event_pv'])['timestamp'],a)
            return controller.acquire(lambda:self.tbt_snapshot(turns),start_turn=start_turn,cancel=cancel)
        controller=TBTController(self.get,self.put,a)
        return controller.acquire(lambda:self.tbt(turns),start_turn=start_turn,cancel=cancel)
    def metadata(self,pv):
        if hasattr(self.client,'ca'):self.client.ca.use_initial_context()
        result=self.client.get_pv(pv,form='time',connect=True).get_with_metadata(use_monitor=False,form='time',timeout=3.)
        if result is None or result.get('value') is None or not np.isfinite(result.get('timestamp',np.nan)):
            raise RuntimeError('PV timestamp/data unavailable: '+pv)
        if result.get('severity',0)>=2:raise RuntimeError('PV alarm: '+pv)
        return result
    def tbt_snapshot(self,turns):
        p=self.profile['tbt'];names=p.get('names') or self.bpm_names;arrays={};evidence={}
        for axis in ('x','y','tmit'):
            pvs=p[axis] if p.get('layout')=='per_bpm' else [p[axis]];columns=[]
            for pv in pvs:
                sample=self.metadata(pv);v=np.asarray(sample['value'],float)
                evidence[pv]=dict(timestamp=float(sample['timestamp']),sha256=hashlib.sha256(v.tobytes()).hexdigest())
                columns.append(v)
            v=np.column_stack(columns) if p.get('layout')=='per_bpm' else columns[0].reshape(p['shape'])
            if p.get('layout')!='per_bpm' and p.get('axes')=='bpm,turn':v=v.T
            if v.shape[0]<turns or v.shape[1]!=len(names):raise ValueError('TBT shape mismatch')
            arrays[axis]=v[:turns]
        scale=float(p['mm_per_unit'])
        if not np.isfinite(scale) or scale<=0:raise ValueError('Invalid TBT units')
        result=dict(names=names,turns=list(range(1,turns+1)),x=arrays['x']*scale,y=arrays['y']*scale,
            tmit=arrays['tmit'],valid=np.isfinite(arrays['x'])&np.isfinite(arrays['y'])&(arrays['tmit']>0),
            position_unit='mm',bpm_noise_rms_mm=p['noise_mm'],orbit_regime='live waveform',equilibrium_validated=False)
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
    def snapshot(self):
        result=super().snapshot();result['quadrupoles']={n:self.quadrupole_current(n) for n in self.quadrupoles};return result
    def same_settings(self,left,right):
        a=left.get('quadrupoles',{});b=right.get('quadrupoles',{})
        return (super().same_settings(left,right) and set(a)==set(b) and
                all(abs(a[n]-b[n])<=self.profile['current_tolerance_A'] for n in a))
    def restore(self,snapshot):
        super().restore(snapshot)
        for n,value in snapshot.get('quadrupoles',{}).items():
            if abs(self.quadrupole_current(n)-value)>self.profile['current_tolerance_A']:self.set_quadrupole_current(n,value)
    def measure_tunes(self,cancel=None):
        p=self.profile.get('qmod_tunes',{})
        if p.get('mode')=='pvs':
            self.validate_tune_measurement()
            initial=[self.metadata(p[a]) for a in ('x','y')];deadline=time.monotonic()+float(p['timeout_s'])
            while True:
                if cancel is not None and cancel.is_set():raise RuntimeError('Tune acquisition cancelled')
                readings=[self.metadata(p[a]) for a in ('x','y')]
                stamps=[r['timestamp'] for r in readings];values=[float(r['value']) for r in readings]
                if (all(stamps[i]>initial[i]['timestamp'] for i in range(2)) and
                    abs(stamps[0]-stamps[1])<=p['max_timestamp_skew_s'] and
                    np.isfinite(values).all() and all(0<v<1 for v in values)):
                    return dict(tunes=values,source='existing TuneDR PVs',pvs=[p['x'],p['y']],timestamps=stamps,
                        same_shot_verified=False,readback_update_verified=True,
                        acquisition_quality='both tune readbacks updated after request; common shot ID unavailable')
                if time.monotonic()>deadline:raise TimeoutError('No fresh H/V tune pair')
                time.sleep(.02)
        wave=self.tbt_acquire(1024,cancel=cancel)
        fit=analyze_tbt(wave,start_turn=wave['turns'][0])
        if not all(fit['planes'][a].get('valid') for a in ('x','y')):
            raise RuntimeError('Fresh TBT must contain measurable H and V excitation for tune scan')
        return dict(tunes=[fit['planes'][a]['tune'] for a in ('x','y')],source='TBT',
                    acquisition_generation=wave.get('acquisition_generation'),
                    same_shot_verified=wave.get('same_shot_verified',wave.get('fresh_acquisition',False)),
                    acquisition_quality=wave.get('acquisition_quality','completion generation'),
                    waveform_evidence=wave.get('waveform_evidence'))
