"""Independent, settings-driven RFTrack optics for measured machine corrections.

The model owns its lattice. It never reads the connected simulator's lattice,
closed orbit or error seed. Currents, calibration and tune targets are explicit
inputs; calculated optics are predictions, never instrument measurements.
"""
import copy
import hashlib
import json
from pathlib import Path
import time
import numpy as np
from scipy.optimize import least_squares
from Knobs.DRCorrection.core import Options, CorrectionService, serial


def name(value):
    return value.removeprefix('dr:').removesuffix('$SKEW')


def digest(value):
    return hashlib.sha256(json.dumps(serial(value), sort_keys=True, allow_nan=False).encode()).hexdigest()


def verify_inputs(backend,settings):
    """Include main supplies and model tune/calibration PVs in stale guards."""
    for pv,expected in settings.get('raw_pvs',{}).items():
        actual=float(backend.get(pv))
        tolerance=1.e-3 if pv.endswith(('currentRead','internalCurrentWrite')) else 1.e-7
        if not np.isfinite(actual) or abs(actual-expected)>tolerance:
            raise ValueError('Model input changed since capture: '+pv+'; rebuild / CALC again')


def linear_map(lattice, state, momentum, step=1.e-4):
    """Eight tracked probes, in RFTrack mm/mrad coordinates."""
    import RF_Track as rft
    offsets=np.vstack((np.eye(4),-np.eye(4)))*step
    coordinates=np.column_stack((np.asarray(state)+offsets,np.zeros(8),np.full(8,momentum)))
    result=lattice.track(rft.Bunch6d(rft.electronmass,0.,-1.,coordinates))
    if result.size()!=8:raise ValueError('Model linear-map probe lost')
    values=np.asarray(result.get_phase_space())[:,:4]
    return ((values[:4]-values[4:])/(2*step)).T


def normal_modes(matrix):
    """Stable symplectic eigenmodes; positive action fixes the tune branch."""
    j=np.kron(np.eye(2),[[0.,1.],[-1.,0.]])
    values,vectors=np.linalg.eig(matrix);modes=[]
    if np.max(abs(abs(values)-1))>2.e-4:raise ValueError('Unstable RFTrack one-turn map')
    for eigenvalue,vector in zip(values,vectors.T):
        action=float(np.imag(np.vdot(vector,j@vector)))
        if action>1.e-8:
            vector=vector*np.sqrt(2/action)
            modes.append((float(np.angle(eigenvalue)/(2*np.pi)%1),vector))
    if len(modes)!=2:raise ValueError('Degenerate model tunes: no two stable modes')
    # Horizontal/vertical assignment is by modal coordinate participation.
    if abs(modes[0][1][0])**2/(abs(modes[0][1][0])**2+abs(modes[0][1][2])**2) < abs(modes[1][1][0])**2/(abs(modes[1][1][0])**2+abs(modes[1][1][2])**2):
        modes.reverse()
    return np.array([m[0] for m in modes]),np.column_stack([m[1] for m in modes])


class OpticsModel:
    """Local actuator adapter, compatible with CorrectionService acquisition."""
    identity='independent-rftrack-model'
    label='RFTrack prediction from recorded settings'
    nominal_frequency_hz=714.e6

    def __init__(self,settings,cancel=lambda:None):
        from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_lattice import build_atf_dr_lattice
        from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_correction import ATFDRRingCorrection
        self.settings=copy.deepcopy(settings);self.check_cancel=cancel
        if settings.get('schema')!='dr-model-settings-1':raise ValueError('Expected dr-model-settings-1')
        self.momentum=float(settings['momentum_mev_c'])
        if not np.isfinite(self.momentum) or self.momentum<=0:raise ValueError('Invalid model momentum')
        self.alpha=float(settings['momentum_compaction'])
        if not np.isfinite(self.alpha) or self.alpha<=0:raise ValueError('Invalid model momentum compaction')
        path=settings.get('lattice_path')
        self.lattice=build_atf_dr_lattice(momentum_mev_c=self.momentum,rf_mode='disabled',lattice_data_path=path)
        self.solver=ATFDRRingCorrection(self.lattice,momentum_mev_c=self.momentum)
        from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_lattice import load_lattice_data
        self.lattice_digest=digest(load_lattice_data(path))
        if settings.get('lattice_sha256') and settings['lattice_sha256']!=self.lattice_digest:
            raise ValueError('Lattice content differs from recorded model settings')
        self.elements={e.get_name():e for e in self.lattice['*']}
        for key,kind in [('quadrupoles_k1l','Quadrupole'),('sextupoles_k2l','Sextupole')]:
            supplied=settings.get(key,{})
            expected={n for n,e in self.elements.items() if type(e).__name__==kind}
            if set(supplied)!=expected:raise ValueError(f'{key}: missing {sorted(expected-set(supplied))}, unknown {sorted(set(supplied)-expected)}')
            for n,k in supplied.items():
                e=self.elements[n]
                if not np.isfinite(k):raise ValueError('Invalid model strength: '+n)
                if kind=='Quadrupole':e.set_K1(-self.momentum,float(k)/e.get_length())
                else:e.set_K2(-self.momentum,float(k)/e.get_length())
        # Optional versioned model-only calibration, never applied to hardware.
        for n,factor in settings.get('quadrupole_factors',{}).items():
            if n not in settings['quadrupoles_k1l'] or not np.isfinite(factor) or factor<=0:raise ValueError('Invalid quadrupole model factor: '+n)
            e=self.elements[n];e.set_K1(-self.momentum,e.get_K1(-self.momentum)*factor)
        for n,k1l in settings.get('bend_k1l',{}).items():
            if n not in self.elements or type(self.elements[n]).__name__!='SBend' or not np.isfinite(k1l):raise ValueError('Invalid bend model setting: '+n)
            self.elements[n].set_K1L(float(k1l))
        self.bpm_names=list(settings['bpm_names']);self.mapping={n:name(n) for n in self.bpm_names}
        missing=[n for n,v in self.mapping.items() if v not in self.solver.bpm_names]
        if missing:raise ValueError('BPMs absent from model; explicitly exclude or supply correct lattice: '+', '.join(missing))
        self.positions_m=np.array([self.elements[self.mapping[n]].get_S('exit') for n in self.bpm_names])
        self.actuators=copy.deepcopy(settings['actuators'])
        self.hcorrectors=[n for n,p in self.actuators.items() if p['kind']=='hcorrector']
        self.vcorrectors=[n for n,p in self.actuators.items() if p['kind']=='vcorrector']
        self.skews=[n for n,p in self.actuators.items() if p['kind']=='skew']
        self.values={n:float(p['value']) for n,p in self.actuators.items()}
        self.offset=float(settings.get('frequency_offset_hz',0.));self._reference_offset=self.offset
        for n,p in self.actuators.items():
            if not np.isfinite([p['scale'],p['value'],*p['limits']]).all() or p['scale']==0 or p['limits'][0]>=p['limits'][1]:raise ValueError('Invalid actuator calibration: '+n)
            self.write('skews' if p['kind']=='skew' else 'correctors',[n],[p['value']])
        self.match_report=None

    def unit(self,kind):
        names=self.skews if kind=='skews' else self.hcorrectors+self.vcorrectors
        units={self.actuators[n]['unit'] for n in names}
        if len(units)!=1:raise ValueError('Mixed or missing actuator units')
        return next(iter(units))
    def limits(self,kind,names=None):
        names=names if names is not None else (self.skews if kind=='skews' else self.hcorrectors+self.vcorrectors)
        return np.array([self.actuators[n]['limits'] for n in names]).T
    def read(self,kind,names):return np.array([self.values[n] for n in names])
    def write(self,kind,names,values):
        self.check_cancel()
        for n,value in zip(names,values):
            p=self.actuators[n];value=float(value)
            if not np.isfinite(value) or not p['limits'][0]<=value<=p['limits'][1]:raise ValueError('Model actuator outside bounds: '+n)
            physical=name(n)
            if p['kind']=='skew':self.solver.set_skew_strength(physical+'$SKEW',value*p['scale'])
            else:
                kick=[0.,0.];kick[int(p['kind']=='vcorrector')]=value*p['scale']
                self.solver._set_corrector_kick(self.solver._single_element(physical),kick)
            self.values[n]=value
    def snapshot(self):return dict(values=self.values.copy(),frequency=self.offset)
    def frequency(self):return self.offset
    def frequency_state(self):return self.offset
    def restore_frequency(self,state):self.set_frequency(state)
    def validate_frequency_targets(self,values):
        if not np.isfinite(values).all():raise ValueError('Invalid model RF offsets')
    def set_frequency(self,value):self.offset=float(value)
    def validate_mode(self,mode):self.check_cancel()
    def momentum_at_offset(self):return self.momentum*(1-(self.offset-self._reference_offset)/(self.nominal_frequency_hz*self.alpha))
    def orbit(self):
        self.check_cancel();result=self.solver.find_closed_orbit(momentum_mev_c=self.momentum_at_offset(),bpm_names=list(self.mapping.values()))
        return dict(names=self.bpm_names,x=result.x,y=result.y,tmit=np.ones(len(self.bpm_names)),valid=np.ones(len(self.bpm_names),bool),position_unit='mm')
    def target_dispersion(self,plane):
        d=self.solver.measure_dispersion(relative_momentum_step=1.e-4,bpm_names=list(self.mapping.values()))
        return d.values[:,0 if plane=='x' else 1] if plane=='x' else np.zeros(len(self.bpm_names))
    def tunes(self):
        self.check_cancel();co=self.solver.find_closed_orbit()
        return normal_modes(linear_map(self.lattice,co.initial_coordinates,self.momentum))[0]
    def match_tunes(self,target,max_scale=.05,tolerance=1.e-5,alias=False):
        target=np.asarray(target,float)%1
        if target.shape!=(2,) or not np.isfinite(target).all():raise ValueError('Two finite tune targets required')
        initial=self.tunes();goals=target.copy()
        if alias:
            goals=np.array([min((t,1-t),key=lambda v:abs((v-q+.5)%1-.5)) for t,q in zip(target,initial)])
        groups=[[e for n,e in self.elements.items() if n.startswith(family+'.') and type(e).__name__=='Quadrupole'] for family in ('QF1R','QF2R')]
        baseline=[[e.get_K1(-self.momentum) for e in group] for group in groups]
        if not all(groups):raise ValueError('QF1R/QF2R model families missing')
        def set_scales(scales):
            for group,ks,scale in zip(groups,baseline,scales):
                for e,k in zip(group,ks):e.set_K1(-self.momentum,k*(1+scale))
        def residual(scales):
            self.check_cancel();set_scales(scales)
            return (self.tunes()-goals+.5)%1-.5
        try:
            result=least_squares(residual,[0.,0.],bounds=(-max_scale,max_scale),diff_step=1.e-4,
                                 xtol=1.e-10,ftol=1.e-10,gtol=1.e-10,max_nfev=40)
            error=residual(result.x)
            if not result.success or np.max(abs(error))>tolerance:raise ValueError(f'Tune match failed: residual {error}; no machine correction calculated')
        except Exception:
            set_scales([0.,0.]);raise
        self.match_report=dict(initial=initial,target=target,resolved_target=goals,final=self.tunes(),
                               family_fractional_changes=result.x,residual=error,source=self.settings.get('tune_source'),model_only=True)
        return self.match_report


def capture_settings(backend,opt,baseline=None,cancel=lambda:None):
    """Only public settings/calibration; no perturbed plant optics or error data."""
    from .calibration import MagnetCalibration,coil_k1l
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_lattice import build_atf_dr_lattice,load_lattice_data
    live=backend.identity.startswith('epics:')
    config=copy.deepcopy(getattr(backend,'model_config',{}))
    if not live and not hasattr(backend,'qcurrents'):raise ValueError('Model response needs RFTrack or Machine interface (DEMO/Stored unsupported)')
    baseline=baseline or backend.snapshot();raw={}
    def read(pv):
        cancel()
        if pv not in raw:
            value=float(backend.get(pv))
            if not np.isfinite(value):raise ValueError('Nonfinite model input: '+pv)
            raw[pv]=value
        return raw[pv]
    energy=read(config.get('energy_pv','SAD:FILE:DR:ENERGY')) if live else 1.2999999
    if energy<=0:raise ValueError('Model energy must be positive')
    p=energy*1000.;path=config.get('lattice_path');design=build_atf_dr_lattice(momentum_mev_c=p,rf_mode='disabled',lattice_data_path=path)
    cal=MagnetCalibration();quads={};sexts={};coil_records={};libraries={}
    for e in design['*']:
        cancel();n=e.get_name();kind=type(e).__name__
        if kind=='Quadrupole':
            if live:
                hardware_name=config.get('quad_mapping',{}).get(n,'QM7R.1' if n=='QM7AR.1' else n)
                entry=backend.quadrupoles.get(hardware_name)
                if entry is None:raise ValueError('Missing live quad coil mapping: '+n)
                c=entry['calibration']
                if c['kind']!='mag_ki_main' or c.get('coil')!='trim':raise ValueError('Full model import requires main + trim mag_ki_main calibration: '+n)
                main=read(c['main_current_pv']);trim=read(entry['read']);mag=c['magnet']
                library=c.get('library');wrapper=cal.mag
                if library:
                    from machine_interfaces_atf2.MagKi import MagKiWrapper
                    if library not in libraries:libraries[library]=MagKiWrapper(library)
                    wrapper=libraries[library]
                quads[n]=coil_k1l(wrapper,mag,energy,main,trim,'main')+coil_k1l(wrapper,mag,energy,main,trim,'trim')
                coil_records[n]=dict(main_A=main,trim_A=trim,magnet=mag,hardware_name=hardware_name,library=library)
            else:
                # Reconstruct from independent public design + commanded trim.
                mag='QM7AR' if n=='QM7R.1' else n.split('.')[0]
                k=e.get_K1L(-p);main=cal.mag.k1l_to_current(mag,k,energy);trim=backend.quadrupole_current(n)
                quads[n]=k+coil_k1l(cal.mag,mag,energy,main,trim,'trim')-coil_k1l(cal.mag,mag,energy,main,0.,'trim')
        elif kind=='Sextupole':
            family=n.split('.')[0]
            sexts[n]=coil_k1l(cal.mag,family,energy,read(family+':internalCurrentWrite')) if live else float(e.get_K2L(-p))
            if live and family=='SD1R':sexts[n]=-abs(sexts[n])  # SetRingSext SAD convention
    scales={}
    if live:
        for family in ('SD','SF'):
            scales[family]=read(config.get('skew_calibration_pvs',{}).get(family,'SAD:SKEWCOR_'+family+'CALIB'))
            if scales[family]==0:raise ValueError('Zero skew calibration: '+family)
    actuators={};unmodelled={};elements={e.get_name() for e in design['*']}
    for kind,names in [('correctors',backend.hcorrectors+backend.vcorrectors),('skews',backend.skews)]:
        lo,hi=backend.limits(kind,names)
        for i,n in enumerate(names):
            cancel();physical=name(n)
            if (physical+'$SKEW' if kind=='skews' else physical) not in elements:
                if n not in opt.fixed_actuators:raise ValueError('Actuator absent from model; explicitly fix it or load matching lattice: '+n)
                unmodelled[n]=dict(value=baseline[kind][n],reason='explicitly fixed; absent from selected lattice')
                continue
            if kind=='skews':scale=scales[physical[:2]] if live else 1.;family='skew'
            else:
                field=cal.mag._call(2,physical,energy,current_main=1.)
                scale=field['field']*field['efflen']*1000/(-p)*.299792458
                family='hcorrector' if n in backend.hcorrectors else 'vcorrector'
            actuators[n]=dict(kind=family,value=baseline[kind][n],unit=backend.unit(kind),scale=scale,limits=[lo[i],hi[i]])
    if live:
        tunes=[read(pv) for pv in config.get('tune_pvs',['SAD:DR_TUNE_X','SAD:DR_TUNE_Y'])]
        tune_source='SAD tune target PVs (not claimed fresh TBT measurements)';alias=False
    else:
        event=getattr(getattr(cancel,'__self__',None),'cancel',None)
        reading=backend.measure_tunes(cancel=event);tunes=reading['tunes']
        tune_source=reading['source'];alias=True
    result=dict(schema='dr-model-settings-1',source=backend.label,captured_unix_s=time.time(),
        momentum_mev_c=p,momentum_compaction=opt.momentum_compaction,lattice_path=path,lattice_sha256=digest(load_lattice_data(path)),
        quadrupoles_k1l=quads,sextupoles_k2l=sexts,quadrupole_factors=config.get('quadrupole_factors',{}),
        calibration_source=config.get('source','mag_ki_main main/trim calibration; SD1R normal-sextupole sign follows SetRingSext'),
        bend_k1l=config.get('bend_k1l',{}),actuators=actuators,frequency_offset_hz=baseline['frequency'],
        bpm_names=[n for n in backend.bpm_names if n not in opt.excluded_bpms],tunes=tunes,tune_alias=alias,
        tune_source=tune_source,raw_pvs=raw,coils=coil_records,
        unmodelled_fixed_actuators=unmodelled,
        assumptions=['Bend geometry and unconfigured bend gradients from versioned lattice; no alignment errors inferred',
                     'RF disabled: transverse periodic optics, not radiation equilibrium'])
    return serial(result)


class ModelManager:
    def __init__(self,backend):
        self.backend=backend;self.settings=None;self.model=None;self.baseline=None;self.optics=None
    def prepare(self,opt,cancel,progress=lambda *_:None):
        progress(0,'Reading public settings for independent RFTrack model')
        self.model=None;self.optics=None
        baseline=self.backend.snapshot()
        settings=capture_settings(self.backend,opt,baseline,cancel)
        model=OpticsModel(settings,cancel)
        progress(10,'Matching model QF1R / QF2R to tune targets (no machine writes)')
        if opt.model_match_tunes:model.match_tunes(settings['tunes'],alias=settings['tune_alias'])
        if not self.backend.same_settings(self.backend.snapshot(),baseline):raise ValueError('Machine settings changed during model preparation')
        verify_inputs(self.backend,settings)
        self.model=model;self.settings=settings;self.baseline=baseline
        return model
    def acquire(self,service,mode,opt,progress):
        b=self.backend;model=self.prepare(opt,service.check_cancel,progress)
        # Compute response before any real RF/probe movement; invalid mappings,
        # unstable optics and calibration errors therefore fail read-only.
        virtual=CorrectionService(model);virtual.cancel=service.cancel
        options=copy.deepcopy(opt);options.response_source='measured';options.bpm_samples=1;options.bpm_interval_s=0.
        progress(15,'Calculating magnet responses in RFTrack; machine magnets unchanged')
        data=virtual.acquire(mode,options,progress)
        if opt.model_fix_extraction_orbit or (mode=='dispersion' and opt.model_fix_extraction_dispersion):
            from .model_optics import extraction_constraints
            data['constraints']=extraction_constraints(model,mode,opt,data,service.check_cancel)
        service.orbit_measurements=[];b.validate_mode(mode)
        if not b.same_settings(b.snapshot(),self.baseline):raise ValueError('Machine settings changed during model response calculation')
        verify_inputs(b,self.settings)
        measured,valid=service._signal(mode,opt);full=service.full_signal.copy()
        index=[b.bpm_names.index(n) for n in model.bpm_names]
        data.update(backend_id=b.identity,baseline=self.baseline,options=vars(opt).copy(),
                    measured=measured[:,index],full_measured=full[:,index],valid=valid[index],
                    orbit_measurements=service.orbit_measurements,source='RFTrack model response + measured signal',
                    model_settings=self.settings,model_settings_sha256=digest(self.settings),
                    tune_match=model.match_report)
        # A model target is legitimate design information, not measured optics.
        if mode=='dispersion' and opt.plane=='x':data['target'][0]=model.target_dispersion('x')
        if opt.model_regional_targets and mode in ('orbit','dispersion') and opt.plane=='x':
            from .model_optics import regional_targets
            data['target']=regional_targets(data,model)
        return data
    def require_current(self):
        if self.model is None:raise ValueError('Build the RFTrack model first')
        if not self.backend.same_settings(self.backend.snapshot(),self.baseline):raise ValueError('Machine settings changed; rebuild RFTrack model')
        verify_inputs(self.backend,self.settings)
        return self.model
