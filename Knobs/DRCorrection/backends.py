"""Explicit demo, existing stored-DR adapter and configured EPICS access.

Importing this module never connects to EPICS or creates a simulator.
"""
import copy
import json
from pathlib import Path
import sys
import time
import numpy as np

HERE=Path(__file__).resolve().parent


class MagnetReadbackError(RuntimeError):
    """A named magnet's current could not be read or did not converge."""
    def __init__(self,names,message):
        super().__init__(message)
        self.names=tuple(names)


class DemoBackend:
    label='DEMO — synthetic response model'
    identity='dr-ui-linear-demo-v1'
    nominal_frequency_hz=714e6
    def __init__(self):
        self.bpm_names=[f'dr:MB{i}R' for i in range(1,97)]
        self.positions_m=np.linspace(0,138.6,96,endpoint=False)
        self.hcorrectors=[f'dr:ZH{i}R' for i in range(1,13)]
        self.vcorrectors=[f'dr:ZV{i}R' for i in range(1,13)]
        self.skews=[f'{family}.{i}$SKEW' for family in ('SD1R','SF1R') for i in range(1,35)]
        self.currents={n:0. for n in self.hcorrectors+self.vcorrectors}
        self.skew_values={n:0. for n in self.skews};self.offset=0.;self.acquisitions=0
        rng=np.random.default_rng(20261021)
        phase=np.arange(96)/96*2*np.pi
        self.basis=np.column_stack([f(k*phase) for k in range(1,5) for f in (np.sin,np.cos)])
        self.orm={axis:self.basis@rng.normal(0,.35,(8,12)) for axis in ('x','y')}
        self.errors={axis:self.basis@rng.normal(0,.08,8) for axis in ('x','y')}
        self.disp0=self.basis@rng.normal(0,3.,8)
        self.dispr=self.basis@rng.normal(0,20.,(8,12))
        self.coupling=self.basis@rng.normal(0,100.,(8,68))
    def validate_mode(self,mode):pass
    def unit(self,kind):return 'm^-1' if kind=='skews' else 'A'
    def limits(self,kind,names=None):
        lo,hi=(-.001,.001) if kind=='skews' else (-5.,5.)
        return (lo,hi) if names is None else (np.full(len(names),lo),np.full(len(names),hi))
    def read(self,kind,names):
        values=self.skew_values if kind=='skews' else self.currents
        return np.array([values[n] for n in names])
    def write(self,kind,names,values):
        target=self.skew_values if kind=='skews' else self.currents
        values=np.asarray(values,float);lo,hi=self.limits(kind)
        if len(names)!=len(values) or len(set(names))!=len(names) or set(names)-set(target) or not np.isfinite(values).all() or np.any(values<lo) or np.any(values>hi):
            raise ValueError('Command outside actuator limits')
        target.update(dict(zip(names,map(float,values))))
    def snapshot(self):return dict(correctors=dict(self.currents),skews=dict(self.skew_values),frequency=self.offset)
    def same_settings(self,left,right):return left==right
    def restore(self,snapshot):
        self.write('correctors',list(snapshot['correctors']),list(snapshot['correctors'].values()))
        self.write('skews',list(snapshot['skews']),list(snapshot['skews'].values()))
        self.set_frequency(snapshot['frequency'])
    def frequency(self):return self.offset
    def frequency_state(self):return self.frequency()
    def restore_frequency(self,state):self.set_frequency(state)
    def validate_frequency_targets(self,values):
        if not np.isfinite(values).all() or np.any(np.abs(values)>5000):raise ValueError('RF offset exceeds +/-5000 Hz')
    def set_frequency(self,value):
        if not np.isfinite(value) or abs(value)>5000:raise ValueError('RF offset exceeds +/-5000 Hz')
        self.offset=float(value)
    def target_dispersion(self,plane):
        return 150*(1+np.cos(np.arange(96)/96*4*np.pi)) if plane=='x' else np.zeros(96)
    def orbit(self):
        self.acquisitions+=1
        h=self.read('correctors',self.hcorrectors);v=self.read('correctors',self.vcorrectors)
        x=self.errors['x']+self.orm['x']@h
        y=self.errors['y']+self.orm['y']@v
        coupled=self.coupling@self.read('skews',self.skews)+self.basis[:,0]*.06
        y+=coupled*(h[0]+.7*h[6])
        delta=-self.offset/(self.nominal_frequency_hz*.0021)
        x+=delta*(self.target_dispersion('x')+self.disp0+self.dispr@h)
        y+=delta*(self.disp0+self.dispr@v)
        return dict(names=self.bpm_names,x=x,y=y,tmit=np.full(96,1e9),valid=np.ones(96,bool),position_unit='mm')
    def tbt(self,turns=1024,pulse=None):
        base=self.orbit();t=np.arange(turns)[:,None];p=np.arange(96)[None,:]
        rng=np.random.default_rng(49+self.acquisitions)
        return dict(names=self.bpm_names,turns=list(range(1,turns+1)),
            x=base['x']+.3*(1+.3*np.sin(p/9))*np.cos(2*np.pi*.205*t+p/8)+rng.normal(0,.005,(turns,96)),
            y=base['y']+.2*(1+.2*np.cos(p/9))*np.cos(2*np.pi*.415*t+p/7)+rng.normal(0,.005,(turns,96)),
            tmit=np.full((turns,96),1e9),valid=np.ones((turns,96),bool),position_unit='mm',
            bpm_noise_rms_mm=.005,orbit_regime='synthetic demo',equilibrium_validated=False)
    def sr_calibration(self):
        # Synthetic display fixture, not the ATF SR optical calibration.
        phases=np.array([.7,1.1]);basis=np.column_stack([f(k*phases) for k in range(1,5) for f in (np.sin,np.cos)])
        return dict(schema='dr-sr-response-1',source='SYNTHETIC DEMO ONLY',names=self.bpm_names,
            monitors=[dict(name='SR demo',s_m=15.44),dict(name='SR2 demo',s_m=24.26)],
            planes={axis:dict(bpm_response=self.basis,monitor_response=basis,
                nominal_bpm=self.target_dispersion('x') if axis=='x' else np.zeros(96),
                nominal_monitor=150*(1+np.cos(2*phases)) if axis=='x' else np.zeros(2)) for axis in ('x','y')})
    def close(self):pass


class StoredBackend(DemoBackend):
    label='SIMULATION — existing 200 ms stored DR'
    sr_calibration=None
    def __init__(self,directory,workspace=None,seed=32):
        root=Path(workspace) if workspace else HERE.parents[2]
        sys.path.insert(0,str(root/'simulation-agent'))
        from dr_stored_interface import StoredDRInterface
        self.interface=StoredDRInterface(dict(seed=seed,measurement_seed=100000+seed,
            alignment_rms_um=5.,initial=str(root/'simulation-agent/results/dr-fast-storage-qualified-20261020/initial_stored_beam.json')),directory)
        self.identity='stored:'+str(Path(directory).resolve())
        c=self.interface.catalog
        self.hcorrectors=c['hcorrectors'];self.vcorrectors=c['vcorrectors'];self.skews=c['motr']['dr_skew_names']
        orbit=self.interface.get_orbit();self.bpm_names=orbit['names']
        # Display geometry is an explicit nominal calibration, not live optics.
        nominal=json.loads((HERE/'forms/bpm_positions.json').read_text())
        self.positions_m=np.array([nominal[n.removeprefix('dr:')] for n in self.bpm_names])
        self.nominal_frequency_hz=c['dr_diagnostics']['nominal_frequency_hz']
        self._target={axis:np.zeros(len(self.bpm_names)) for axis in ('x','y')}
        # Horizontal target must be explicitly supplied; do not replace it
        # with a dispersion measured from the perturbed plant.
        self.has_horizontal_dispersion_target=False
    def validate_mode(self,mode):pass
    def read(self,kind,names):
        method=self.interface.get_skew_correctors if kind=='skews' else self.interface.get_correctors
        return np.asarray(method(names)['bdes'])
    def write(self,kind,names,values):
        method=self.interface.set_skew_correctors if kind=='skews' else self.interface.set_correctors
        method(names,list(map(float,values)))
    def snapshot(self):
        read=self.interface._rpc('readbacks')
        return dict(correctors=read['correctors'],skews=read['dr_skew_k1l'],frequency=read['dr_frequency_offset_hz'])
    def frequency(self):return self.interface.get_dr_frequency_offset_hz()
    def set_frequency(self,value):return self.interface.set_dr_frequency_offset_hz(float(value))
    def target_dispersion(self,plane):
        if plane=='x' and not self.has_horizontal_dispersion_target:
            raise ValueError('Load a public nominal horizontal dispersion target for this lattice')
        return self._target[plane].copy()
    def orbit(self):return self.interface.get_orbit()
    def tbt(self,turns=1024,pulse=None):return self.interface.get_tbt(turns,pulse=pulse)
    def close(self):self.interface.close()


class EpicsBackend(DemoBackend):
    """Device PVs are configuration, never inferred from SAD parameter PVs."""
    label='EPICS — configured machine'
    sr_calibration=None
    def __init__(self,profile,client=None):
        self.profile=copy.deepcopy(profile)
        missing=profile_issues(profile)
        if missing:raise ValueError('Incomplete hardware profile:\n'+'\n'.join(missing))
        if client is None:
            import epics
            client=epics
        self.client=client
        self.identity='epics:'+profile['machine_id']
        self.bpm_names=profile['bpm_names'];self.positions_m=np.asarray(profile['positions_m'],float)
        self.hcorrectors=profile['hcorrectors'];self.vcorrectors=profile['vcorrectors'];self.skews=profile['skews']
        self.nominal_frequency_hz=profile['nominal_frequency_hz']
        self.mapping=profile['actuators'];self.acquisitions=0
        self.ramp=None
        if profile['rf'].get('mode')=='delta_ramp':
            from .ramp import DeltaRamp
            self.ramp=DeltaRamp(self.get,self.put,profile['rf'])
    def get(self,pv):
        value=self.client.caget(pv,timeout=3.,use_monitor=False)
        if value is None:raise RuntimeError('PV read failed: '+pv)
        return value
    def put(self,pv,value):
        status=self.client.caput(pv,value,wait=True,timeout=3.)
        if status!=1:raise RuntimeError('PV write failed: '+pv)
    def unit(self,kind):return 'A'
    def limits(self,kind,names=None):
        selected=list(names) if names is not None else (self.skews if kind=='skews' else self.hcorrectors+self.vcorrectors)
        lower=[];upper=[]
        for name in selected:
            entry=self.mapping[name]
            if entry.get('lower_limit_pv') or entry.get('upper_limit_pv'):
                lo=float(self.get(entry['lower_limit_pv']));hi=float(self.get(entry['upper_limit_pv']))
            else:lo,hi=self.profile['skew_limits_A'] if kind=='skews' else (-5.,5.)
            if not np.isfinite([lo,hi]).all() or lo>=hi:raise ValueError('Invalid current-limit PVs for '+name)
            if kind!='skews':lo=max(lo,-5.);hi=min(hi,5.)
            if lo>=hi:raise ValueError('No permitted current range for '+name)
            lower.append(lo);upper.append(hi)
        if names is None:return max(lower),min(upper)
        return np.asarray(lower),np.asarray(upper)
    def magnet_readback(self,name,pv):
        try:
            value=float(self.get(pv))
            if not np.isfinite(value):raise ValueError('Nonfinite magnet readback')
        except (RuntimeError,ValueError,TypeError,TimeoutError) as exc:
            raise MagnetReadbackError([name],f'{name}: read PV={pv}: {exc}') from exc
        return value
    def read(self,kind,names):
        return np.array([self.magnet_readback(n,self.mapping[n]['read']) for n in names])
    def restore_probe(self,kind,name,value):
        # The scan knows the original current. Attempt that command even if
        # readback disappeared; write() normally requires a readable old value.
        lo,hi=self.limits(kind,[name])
        if not np.isfinite(value) or not lo[0]<=value<=hi[0]:raise ValueError('Invalid probe restoration current')
        settle=float(self.profile.get('magnet_settle_s',0.))
        if not np.isfinite(settle) or settle<0:raise ValueError('Invalid magnet_settle_s')
        self.put(self.mapping[name]['set'],float(value))
        deadline=time.monotonic()+float(self.profile['readback_timeout_s'])
        while True:
            reading=self.read(kind,[name])[0]
            if abs(reading-value)<=self.profile['current_tolerance_A']:break
            if time.monotonic()>=deadline:
                raise MagnetReadbackError([name],f'{name}: probe restoration did not settle; target={value:g} A, readback={reading:g} A')
            time.sleep(.05)
        if settle:time.sleep(settle)
        reading=self.read(kind,[name])[0]
        if abs(reading-value)>self.profile['current_tolerance_A']:
            raise MagnetReadbackError([name],f'{name}: probe restoration readback changed during settling wait; target={value:g} A, readback={reading:g} A')
    def write(self,kind,names,values):
        values=np.asarray(values,float);lo,hi=self.limits(kind,names)
        settle=float(self.profile.get('magnet_settle_s',0.))
        if not np.isfinite(settle) or settle<0:raise ValueError('Invalid magnet_settle_s')
        available=self.skews if kind=='skews' else self.hcorrectors+self.vcorrectors
        if len(names)!=len(values) or len(set(names))!=len(names) or set(names)-set(available) or not np.isfinite(values).all() or np.any(values<lo) or np.any(values>hi):
            raise ValueError('Command outside current limits')
        if not names:return
        old=self.read(kind,names)
        tolerance=float(self.profile['current_tolerance_A'])
        timeout=float(self.profile['readback_timeout_s'])
        def details(targets,readings):
            return '; '.join(f'{n}: set PV={self.mapping[n]["set"]}, read PV={self.mapping[n]["read"]}, '
                f'target={v:.9g} A, readback={r:.9g} A, error={r-v:+.9g} A'
                for n,v,r in zip(names,targets,readings) if not np.isfinite(r) or abs(r-v)>tolerance)
        def wait(targets):
            deadline=time.monotonic()+timeout
            while True:
                readings=self.read(kind,names)
                if np.max(abs(readings-targets))<=tolerance:return
                if time.monotonic()>=deadline:
                    raise MagnetReadbackError(names,f'Magnet readback did not settle after {timeout:g} s '
                        f'(tolerance {tolerance:g} A): '+details(targets,readings))
                time.sleep(.05)
        try:
            for n,v in zip(names,values):self.put(self.mapping[n]['set'],float(v))
            wait(values)
            # Supply readback convergence is not BPM acquisition completion.
            # Allow the beam/monitor chain to settle before the caller reads it.
            if settle:time.sleep(settle)
            readings=self.read(kind,names)
            if np.max(abs(readings-values))>tolerance:
                raise MagnetReadbackError(names,'Magnet readback changed during settling wait: '+details(values,readings))
        except Exception as exc:
            # Attempt every restoration, and never hide the original failure.
            failures=[]
            for n,v in zip(names,old):
                try:self.put(self.mapping[n]['set'],float(v))
                except Exception as restore_error:failures.append(str(restore_error))
            try:wait(old)
            except Exception as restore_error:failures.append(str(restore_error))
            restoration='Restoration FAILED: '+'; '.join(failures) if failures else 'Original readback currents restored and verified.'
            if isinstance(exc,MagnetReadbackError):
                raise MagnetReadbackError(exc.names,str(exc)+'\n'+restoration) from exc
            raise RuntimeError(str(exc)+'\n'+restoration) from exc
    def snapshot_magnets(self,names,read,errors=None,excluded=()):
        result={}
        for name in names:
            if name in excluded:continue
            try:result[name]=read(name)
            except MagnetReadbackError as exc:
                if errors is None:raise
                errors.append(dict(magnet=name,stage='initial readback',error=str(exc),
                    restoration='not moved',restored=True))
        return result
    def measured_snapshot(self,errors):
        return self.snapshot(readback_errors=errors)
    def snapshot(self,readback_errors=None,excluded=()):
        result={kind:self.snapshot_magnets(names,lambda n:self.read(kind,[n])[0],readback_errors,excluded)
            for kind,names in [('correctors',self.hcorrectors+self.vcorrectors),('skews',self.skews)]}
        result['frequency']=self.frequency()
        if self.ramp:result['ramp_state']=self.ramp.snapshot()
        return result
    def restore(self,snapshot):
        for kind in ('correctors','skews'):self.write(kind,list(snapshot[kind]),list(snapshot[kind].values()))
        if self.ramp:self.restore_frequency(snapshot['ramp_state'])
        else:self.set_frequency(snapshot['frequency'])
    def same_settings(self,left,right):
        if self.ramp:
            a=left.get('ramp_state',{});b=right.get('ramp_state',{})
            if any(a.get(k)!=b.get(k) for k in ('enabled','offset_hz')):return False
            readings=[a.get('read_hz',float('nan')),b.get('read_hz',float('nan'))]
            if not np.isfinite(readings).all() or abs(readings[0]-readings[1])>self.profile['rf']['tolerance_hz']:return False
        for kind in ('correctors','skews'):
            if set(left[kind])!=set(right[kind]):return False
            if not all(np.isfinite(left[kind][n]) and abs(left[kind][n]-right[kind][n])<=self.profile['current_tolerance_A'] for n in left[kind]):return False
        return np.isfinite(left['frequency']) and abs(left['frequency']-right['frequency'])<=self.profile['rf']['tolerance_hz']
    def frequency(self):
        if self.ramp:return self.ramp.state()['offset_hz']
        value=float(self.get(self.profile['rf']['read']))*self.profile['rf']['hz_per_unit']-self.nominal_frequency_hz
        if not np.isfinite(value):raise RuntimeError('Nonfinite RF frequency readback')
        return value
    def frequency_state(self):return self.ramp.snapshot() if self.ramp else self.frequency()
    def restore_frequency(self,state):
        if self.ramp:self.ramp.restore(state)
        else:self.set_frequency(state)
    def validate_frequency_targets(self,values):
        super().validate_frequency_targets(values)
        if self.ramp:
            for value in values:self.ramp.label(value)
    def set_frequency(self,value):
        if not np.isfinite(value) or abs(value)>5000:raise ValueError('RF offset outside limits')
        if self.ramp:return self.ramp.set(value)
        rf=self.profile['rf'];old=self.frequency()
        try:
            self.put(rf['set'],(self.nominal_frequency_hz+value)/rf['hz_per_unit'])
            time.sleep(rf['settle_s'])
            reading=self.frequency()
            if not np.isfinite(reading) or abs(reading-value)>rf['tolerance_hz']:raise RuntimeError('RF readback mismatch')
        except Exception:
            self.put(rf['set'],(self.nominal_frequency_hz+old)/rf['hz_per_unit']);raise
    def orbit(self):
        p=self.profile['orbit']
        if p.get('mode_read') and self.get(p['mode_read'])!=p['required_mode']:
            raise RuntimeError('BPM mode is not the configured COD mode')
        if p.get('packed_pv'):
            raw=np.asarray(self.get(p['packed_pv']),float).reshape((-1,p['row_width']))
            raw=raw[np.asarray(p['row_indices'],int)]
            values={axis:raw[:,p['columns'][axis]] for axis in ('x','y','tmit')}
            status=raw[:,p['columns']['status']]==p['valid_status']
            status_values,status_counts=np.unique(raw[:,p['columns']['status']],return_counts=True)
            diagnostic=dict(pv=p['packed_pv'],required_status=p['valid_status'],
                status_counts={str(v):int(c) for v,c in zip(status_values,status_counts)})
        else:
            values={axis:np.asarray(self.get(p[axis]),float) for axis in ('x','y','tmit')}
            status=np.ones(len(self.bpm_names),bool)
            diagnostic=dict(pv=', '.join(p[k] for k in ('x','y','tmit')))
        if any(v.shape!=(len(self.bpm_names),) for v in values.values()):raise ValueError('BPM waveform length/order differs from profile')
        self.acquisitions+=1
        return dict(names=self.bpm_names,x=values['x']*p['mm_per_unit'],y=values['y']*p['mm_per_unit'],
                    tmit=values['tmit'],valid=status&np.isfinite(values['x'])&np.isfinite(values['y'])&(values['tmit']>0),
                    validity_diagnostics=diagnostic,position_unit='mm')
    def target_dispersion(self,plane):return np.asarray(self.profile['target_dispersion_mm'][plane],float)
    def tbt(self,turns=1024,pulse=None):
        if pulse:raise ValueError('Live exciter is not configured; use an externally acquired waveform')
        p=self.profile.get('tbt')
        if not p or any(not p.get(k) for k in ('x','y','tmit')):raise ValueError('Configure live TBT waveform PVs and array order')
        if not p.get('mapping_confirmed'):raise ValueError('Confirm TBT channel-to-BPM mapping and waveform units first')
        names=p.get('names') or self.bpm_names
        if len(set(names))!=len(names):raise ValueError('Duplicate TBT BPM names')
        if p.get('layout')=='per_bpm':
            arrays={axis:np.column_stack([np.asarray(self.get(pv),float) for pv in p[axis]]) for axis in ('x','y','tmit')}
        else:
            arrays={axis:np.asarray(self.get(p[axis]),float).reshape(p['shape']) for axis in ('x','y','tmit')}
            if p.get('axes')=='bpm,turn':arrays={k:v.T for k,v in arrays.items()}
        if any(v.shape[0]<turns or v.shape[1]!=len(names) for v in arrays.values()):raise ValueError('TBT shape mismatch')
        if p.get('mm_per_unit') is None or not np.isfinite(p['mm_per_unit']) or p['mm_per_unit']<=0:raise ValueError('Invalid TBT position units')
        arrays={k:v[:turns] for k,v in arrays.items()}
        return dict(names=names,turns=list(range(1,turns+1)),x=arrays['x']*p['mm_per_unit'],
            y=arrays['y']*p['mm_per_unit'],tmit=arrays['tmit'],valid=np.isfinite(arrays['x'])&np.isfinite(arrays['y'])&(arrays['tmit']>0),
            position_unit='mm',bpm_noise_rms_mm=p['noise_mm'],orbit_regime='live waveform',equilibrium_validated=False)


def profile_issues(p):
    issues=[]
    if p.get('rf',{}).get('mode','direct') not in ('direct','delta_ramp'):issues.append('Unknown RF control mode')
    for key in ('machine_id','bpm_names','positions_m','hcorrectors','vcorrectors','skews','nominal_frequency_hz','actuators','orbit','rf','target_dispersion_mm','readback_timeout_s','current_tolerance_A'):
        if not p.get(key):issues.append('Missing '+key)
    if not p.get('bpm_mapping_confirmed'):issues.append('Confirm BPM name/order and position mapping')
    orbit_keys=('packed_pv','row_width','row_indices','columns','valid_status','mm_per_unit') if p.get('orbit',{}).get('packed_pv') else ('x','y','tmit','mm_per_unit')
    rf_keys=('read','hz_per_unit','settle_s','tolerance_hz','timeout_s') if p.get('rf',{}).get('mode')=='delta_ramp' else ('set','read','hz_per_unit','settle_s','tolerance_hz')
    for section,keys in [('orbit',orbit_keys),('rf',rf_keys)]:
        for key in keys:
            if p.get(section,{}).get(key) is None:issues.append(f'Missing {section}.{key}')
    for name in p.get('hcorrectors',[])+p.get('vcorrectors',[])+p.get('skews',[]):
        for key in ('set','read'):
            if not p.get('actuators',{}).get(name,{}).get(key):issues.append(f'Missing actuator {name}.{key}')
        entry=p.get('actuators',{}).get(name,{})
        if bool(entry.get('lower_limit_pv'))!=bool(entry.get('upper_limit_pv')):issues.append('Both current-limit PVs required for '+name)
    n=len(p.get('bpm_names',[]))
    orbit=p.get('orbit',{})
    if orbit.get('packed_pv'):
        try:
            indices=orbit['row_indices'];width=orbit['row_width'];columns=orbit['columns']
            if len(indices)!=n or len(set(indices))!=n or any(int(v)!=v or v<0 for v in indices):raise ValueError()
            if width<4 or int(width)!=width or any(int(columns[k])!=columns[k] or not 0<=columns[k]<width for k in ('x','y','tmit','status')):raise ValueError()
        except (ValueError,TypeError,KeyError):issues.append('Invalid packed BPM waveform layout')
    if p.get('rf',{}).get('control_scheme_confirmed') is False:issues.append('Confirm DR RF control scheme (independent or linked LN/DR/water ramp)')
    def positive(label,value,zero=False):
        try:ok=np.isfinite(float(value)) and (float(value)>=0 if zero else float(value)>0)
        except (TypeError,ValueError):ok=False
        if not ok:issues.append('Invalid '+label)
    for key in ('nominal_frequency_hz','readback_timeout_s','current_tolerance_A'):positive(key,p.get(key))
    positive('magnet_settle_s',p.get('magnet_settle_s',0.),True)
    for section,key,zero in [('orbit','mm_per_unit',False),('rf','hz_per_unit',False),('rf','settle_s',True),('rf','tolerance_hz',False)]:positive(section+'.'+key,p.get(section,{}).get(key),zero)
    if p.get('rf',{}).get('mode')=='delta_ramp':
        positive('rf.timeout_s',p['rf'].get('timeout_s'))
        positive('rf.enable_settle_s',p['rf'].get('enable_settle_s',1.),True)
    for key in ('bpm_names','hcorrectors','vcorrectors','skews'):
        names=p.get(key,[])
        if len(set(names))!=len(names) or any(not isinstance(v,str) or not v for v in names):issues.append('Invalid names in '+key)
    all_magnets=p.get('hcorrectors',[])+p.get('vcorrectors',[])+p.get('skews',[])
    if len(set(all_magnets))!=len(all_magnets):issues.append('Overlapping actuator families')
    for label,value in [('positions_m',p.get('positions_m'))]+[(f'target_dispersion_mm.{axis}',p.get('target_dispersion_mm',{}).get(axis)) for axis in ('x','y')]:
        if label.endswith('.x') and value is None:continue  # can be loaded before horizontal CALC
        try:
            if label=='positions_m' and p.get('allow_missing_positions'):
                # Missing geometry does not invalidate a measured BPM response.
                # Keep missing s as null/NaN; plotting uses an explicit index axis.
                ok=np.shape(value)==(n,) and all(v is None or np.isfinite(float(v)) for v in value)
            else:ok=np.shape(value)==(n,) and np.isfinite(np.asarray(value,float)).all()
        except (TypeError,ValueError):ok=False
        if not ok:issues.append('Invalid '+label+' array')
    if any(not all(p.get('actuators',{}).get(n,{}).get(k) for k in ('lower_limit_pv','upper_limit_pv')) for n in p.get('skews',[])):
        try:
            limits=np.asarray(p.get('skew_limits_A'),float)
            if limits.shape!=(2,) or not np.isfinite(limits).all() or limits[0]>=limits[1]:raise ValueError()
        except (TypeError,ValueError):issues.append('Provide skew limit PVs or finite fallback limits in A')
    return issues
