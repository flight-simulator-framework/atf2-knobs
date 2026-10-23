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
from scipy.optimize import lsq_linear
from Knobs.DRCorrection.core import Options, CorrectionService, serial


def name(value):
    return value.removeprefix('dr:').removesuffix('$SKEW')


def digest(value):
    return hashlib.sha256(json.dumps(serial(value), sort_keys=True, allow_nan=False).encode()).hexdigest()


def preparation_config(backend):
    config=copy.deepcopy(getattr(backend,'model_config',{}))
    preset=config.get('preparation','sad_cod' if backend.identity.startswith('epics:') else 'all_currents')
    if preset not in ('sad_cod','all_currents'):raise ValueError('Unknown model preparation: '+str(preset))
    config['preparation']=preset
    bound=float(config.get('tune_match_max_scale',.05))
    if not np.isfinite(bound) or not 0<bound<=.2:raise ValueError('Model QF matching limit must be in (0, 20%]')
    config['tune_match_max_scale']=bound
    if preset=='sad_cod' and ('quadrupole_factors' not in config or 'bend_k1l' not in config):
        defaults=json.loads(Path(__file__).with_name('sad_cod_calibration.json').read_text())
        for key in ('quadrupole_factors','bend_k1l','source','source_sha256'):
            config.setdefault(key,defaults[key])
    return config


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


class ModelOpticsError(ValueError):
    def __init__(self,message,diagnostics):
        super().__init__(message);self.diagnostics=diagnostics


def normal_modes(matrix):
    """Stable symplectic eigenmodes; positive action fixes the tune branch."""
    j=np.kron(np.eye(2),[[0.,1.],[-1.,0.]])
    matrix=np.asarray(matrix,float)
    if matrix.shape!=(4,4) or not np.isfinite(matrix).all():raise ValueError('Invalid RFTrack one-turn map (expected finite 4x4)')
    values,vectors=np.linalg.eig(matrix);modes=[]
    deviation=float(np.max(abs(abs(values)-1)))
    diagnostics=dict(one_turn_matrix=matrix,eigenvalues_real=values.real,eigenvalues_imag=values.imag,
                     eigenvalue_moduli=abs(values),max_modulus_deviation=deviation,stability_tolerance=2.e-4,
                     determinant=float(np.linalg.det(matrix)),symplectic_residual=float(np.max(abs(matrix.T@j@matrix-j))))
    if deviation>2.e-4:
        raise ModelOpticsError('Unstable RFTrack one-turn map: max ||lambda|-1|='
            f'{deviation:.6g} (limit 0.0002); |lambda|='+', '.join(f'{v:.6g}' for v in abs(values)),diagnostics)
    for eigenvalue,vector in zip(values,vectors.T):
        action=float(np.imag(np.vdot(vector,j@vector)))
        if action>1.e-8:
            vector=vector*np.sqrt(2/action)
            modes.append((float(np.angle(eigenvalue)/(2*np.pi)%1),vector))
    if len(modes)!=2:raise ModelOpticsError('Degenerate model tunes: no two stable modes',diagnostics)
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
        import RF_Track as rft
        # Orbit/map calculations track only a handful of particles. Match the
        # DR RFTrack backend; large thread pools overwhelm these small probes.
        rft.number_of_threads=1
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
            # SAD COD uses zero steerers and no imported skew excitation.
            # Keep absolute machine currents/limits in the adapter; only the
            # MODEL kick is relative to the captured current. Thus SET remains
            # I_machine + delta, never delta about zero machine current.
            excitation=value-p['value'] if self.settings.get('preparation')=='sad_cod' else value
            if p['kind']=='skew':self.solver.set_skew_strength(physical+'$SKEW',excitation*p['scale'])
            else:
                kick=[0.,0.];kick[int(p['kind']=='vcorrector')]=excitation*p['scale']
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
        if not np.isfinite(max_scale) or max_scale<=0:raise ValueError('Positive finite model matching bound required')
        groups=[[e for n,e in self.elements.items() if n.startswith(family+'.') and type(e).__name__=='Quadrupole'] for family in ('QF1R','QF2R')]
        baseline=[[e.get_K1(-self.momentum) for e in group] for group in groups]
        if not all(groups):raise ValueError('QF1R/QF2R model families missing')
        def set_scales(scales):
            for group,ks,scale in zip(groups,baseline,scales):
                for e,k in zip(group,ks):e.set_K1(-self.momentum,k*(1+scale))
        rejected=[];seed_report=None;history=[];accepted_points=[]
        self.match_report=None
        self.match_attempt=dict(status='running',target=target.copy(),alias=bool(alias),
            max_scale=max_scale,tolerance=tolerance,model_only=True,history=history,
            accepted_points=accepted_points,rejected_unstable_trials=rejected)
        def initial_point():
            nonlocal seed_report
            try:return np.zeros(2),self.tunes()
            except ModelOpticsError as failure:
                # The SAD COD reference has zero steerers/skews. An imported
                # working point just outside a stable band cannot provide tune
                # derivatives. Find a nearby stable MODEL point first, keeping
                # all family changes bounded relative to the captured inputs.
                if self.settings.get('preparation')!='sad_cod':raise
                original=serial(failure.diagnostics);evaluations=0
                radii=sorted(set(min(max_scale,r) for r in (1.e-4,2.5e-4,5.e-4,.001,.0025,.005,.01,.025,.05,max_scale)))
                for radius in radii:
                    candidates=[]
                    for x,y in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
                        self.check_cancel();point=radius*np.array([x,y]);set_scales(point);evaluations+=1
                        try:q=self.tunes()
                        except ModelOpticsError as exc:
                            rejected.append(serial(dict(exc.diagnostics,stage='initial stable point search',
                                tune_match_trial_fractional_changes=point)))
                            continue
                        goal=target if not alias else np.array([min((t,1-t),key=lambda v:abs((v-a+.5)%1-.5)) for t,a in zip(target,q)])
                        history.append(dict(stage='stable point search',family_fractional_changes=point.copy(),
                            tunes=q.copy(),resolved_target=goal.copy(),residual=(q-goal+.5)%1-.5))
                        candidates.append((np.linalg.norm((q-goal+.5)%1-.5),point,q))
                    if candidates:
                        _,point,q=min(candidates,key=lambda entry:entry[0]);set_scales(point)
                        seed_report=dict(initial_optics_failure=original,evaluations=evaluations,
                            family_fractional_changes=point.copy(),tunes=q.copy(),max_scale=max_scale,
                            method='expanding bounded QF1R/QF2R grid; stable point required')
                        self.match_attempt['initial_stable_point_search']=seed_report
                        return point,q
                raise ModelOpticsError('No stable initial RFTrack model found in bounded QF1R/QF2R search '
                    f'(+/-{max_scale:.1%}, {evaluations} trials); inspect imported currents/calibration/lattice. '+str(failure),
                    dict(original,stable_point_search_evaluations=evaluations,stable_point_search_max_scale=max_scale,
                         rejected_unstable_trials=rejected)) from failure
        def residual(scales):
            self.check_cancel();set_scales(scales)
            try:
                tunes=self.tunes();error=(tunes-goals+.5)%1-.5
                history.append(dict(stage='tune matching',family_fractional_changes=np.asarray(scales).copy(),
                    tunes=tunes.copy(),resolved_target=goals.copy(),residual=error.copy()))
                return error
            except ModelOpticsError as exc:
                detail=dict(exc.diagnostics,tune_match_trial_fractional_changes=np.asarray(scales).copy(),resolved_target=goals)
                raise ModelOpticsError('Tune-match trial failed at QF1R/QF2R changes '
                                       +', '.join(f'{v:+.6%}' for v in scales)+': '+str(exc),detail) from exc
        def trial(scales):
            try:return residual(scales)
            except ModelOpticsError as exc:
                rejected.append(serial(exc.diagnostics));return None
        try:
            scales,initial=initial_point();goals=target.copy()
            if alias:
                goals=np.array([min((t,1-t),key=lambda v:abs((v-q+.5)%1-.5)) for t,q in zip(target,initial)])
            error=residual(scales)
            self.match_attempt.update(initial=initial.copy(),resolved_target=goals.copy())
            accepted_points.append(dict(family_fractional_changes=scales.copy(),residual=error.copy()))
            # Absolute family perturbations avoid the tiny relative steps near
            # zero used by generic optimizers. Reject unstable trials and back
            # track from the last stable iterate, keeping the stability test.
            for iteration in range(40):
                if np.max(abs(error))<=tolerance:break
                columns=[]
                for axis in range(2):
                    column=None
                    for h in (1.e-4,-1.e-4,1.e-5,-1.e-5):
                        point=scales.copy();point[axis]+=h
                        if np.max(abs(point))>max_scale:continue
                        probe=trial(point)
                        if probe is not None:
                            column=((probe-error+.5)%1-.5)/h;break
                    if column is None:raise ValueError('Tune match could not form a stable local response')
                    columns.append(column)
                jacobian=np.column_stack(columns)
                # Solve inside the remaining family bounds. Clipping an
                # unconstrained Newton step can discard its descent direction
                # at a bound and miss improvement in the other family.
                lower=np.maximum(-max_scale-scales,-.005)
                upper=np.minimum(max_scale-scales,.005)
                fit=lsq_linear(jacobian,-error,bounds=(lower,upper),tol=1.e-12)
                if not fit.success:raise ValueError('Bounded local tune step failed: '+fit.message)
                step=fit.x
                accepted=False
                for power in range(16):
                    point=np.clip(scales+step*(.5**power),-max_scale,max_scale)
                    probe=trial(point)
                    if probe is not None and np.linalg.norm(probe)<np.linalg.norm(error):
                        scales=point;error=probe;accepted=True
                        accepted_points.append(dict(family_fractional_changes=scales.copy(),residual=error.copy()))
                        break
                if not accepted:break
            error=residual(scales)
            self.match_attempt.update(final=history[-1]['tunes'].copy(),residual=error.copy(),
                family_fractional_changes=scales.copy(),iterations=iteration+1,
                bound_active=np.isclose(abs(scales),max_scale,rtol=0,atol=1.e-10))
            if np.max(abs(error))>tolerance:
                raise ValueError(f'Tune match failed: residual {error}; QF changes {scales}; '
                    f'target {goals}; model tunes {history[-1]["tunes"]}; '
                    f'model QF limit +/-{max_scale:.1%}; bound active={self.match_attempt["bound_active"].tolist()}; '
                    f'rejected unstable trials={len(rejected)}; no machine correction calculated')
        except Exception as exc:
            self.match_attempt.update(status='failed',error=str(exc),exception_type=type(exc).__name__)
            set_scales([0.,0.]);self.match_attempt['model_restored']=True
            raise
        self.match_attempt['status']='ready'
        self.match_report=dict(initial=initial,target=target,resolved_target=goals,final=self.tunes(),
                               family_fractional_changes=scales,residual=error,source=self.settings.get('tune_source'),model_only=True,
                               iterations=iteration+1,rejected_unstable_trials=rejected,initial_stable_point_search=seed_report,
                               max_scale=max_scale,method='bounded least-squares Newton with stable backtracking')
        return self.match_report


def capture_settings(backend,opt,baseline=None,cancel=lambda:None):
    """Only public settings/calibration; no perturbed plant optics or error data."""
    from .calibration import MagnetCalibration,coil_k1l
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_lattice import build_atf_dr_lattice,load_lattice_data
    live=backend.identity.startswith('epics:')
    config=preparation_config(backend)
    sad=config['preparation']=='sad_cod'
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
    cal=MagnetCalibration();quads={};sexts={};coil_records={};libraries={};design_quads={}
    for e in design['*']:
        cancel();n=e.get_name();kind=type(e).__name__
        if kind=='Quadrupole':
            design_quads[n]=float(e.get_K1L(-p))
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
                include_trim=not sad or (n.startswith('QM') and not n.startswith('QM7'))
                main_k=coil_k1l(wrapper,mag,energy,main,trim,'main')
                trim_k=coil_k1l(wrapper,mag,energy,main,trim,'trim')
                quads[n]=main_k+(trim_k if include_trim else 0.)
                coil_records[n]=dict(main_A=main,trim_A=trim,main_k1l=main_k,trim_k1l=trim_k,
                    trim_included=include_trim,magnet=mag,hardware_name=hardware_name,library=library)
            else:
                # Reconstruct from independent public design + commanded trim.
                mag='QM7AR' if n=='QM7R.1' else n.split('.')[0]
                k=e.get_K1L(-p);main=cal.mag.k1l_to_current(mag,k,energy);trim=backend.quadrupole_current(n)
                include_trim=not sad or (n.startswith('QM') and not n.startswith('QM7'))
                quads[n]=k+(coil_k1l(cal.mag,mag,energy,main,trim,'trim')-coil_k1l(cal.mag,mag,energy,main,0.,'trim') if include_trim else 0.)
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
        preparation=config['preparation'],tune_match_max_scale=config['tune_match_max_scale'],
        calibration_source_sha256=config.get('source_sha256'),
        reference_excitation='zero correctors and skews in model; absolute machine centers retained' if sad else 'captured corrector and skew currents',
        momentum_mev_c=p,momentum_compaction=opt.momentum_compaction,lattice_path=path,lattice_sha256=digest(load_lattice_data(path)),
        quadrupoles_k1l=quads,sextupoles_k2l=sexts,quadrupole_factors=config.get('quadrupole_factors',{}),
        calibration_source=config.get('source','mag_ki_main main/trim calibration; SD1R normal-sextupole sign follows SetRingSext'),
        bend_k1l=config.get('bend_k1l',{}),actuators=actuators,frequency_offset_hz=baseline['frequency'],
        bpm_names=[n for n in backend.bpm_names if n not in opt.excluded_bpms],tunes=tunes,tune_alias=alias,
        tune_source=tune_source,raw_pvs=raw,coils=coil_records,
        design_quadrupoles_k1l=design_quads,
        unmodelled_fixed_actuators=unmodelled,
        assumptions=['Bend geometry and unconfigured bend gradients from versioned lattice; no alignment errors inferred',
                     'RF disabled: transverse periodic optics, not radiation equilibrium'])
    return serial(result)


class ModelManager:
    def __init__(self,backend):
        self.backend=backend;self.settings=None;self.model=None;self.baseline=None;self.optics=None
        self.last_attempt=None
    def prepare(self,opt,cancel,progress=lambda *_:None):
        progress(0,'Reading public settings for independent RFTrack model')
        self.model=None;self.optics=None;self.settings=None;self.baseline=None
        attempt=dict(schema='dr-model-diagnostic-1',captured_unix_s=time.time(),stage='reading settings',
                     status='running',options=vars(opt).copy(),settings=None)
        self.last_attempt=attempt
        model=None
        try:
            baseline=self.backend.snapshot();attempt['baseline']=baseline
            settings=capture_settings(self.backend,opt,baseline,cancel);attempt['settings']=settings
            attempt['stage']='constructing imported model';model=OpticsModel(settings,cancel)
            bound=float(settings.get('tune_match_max_scale',.05))
            attempt['stage']='initial imported optics stability check'
            progress(5,'Checking initial imported model stability (no machine writes)')
            try:attempt['initial_tunes']=model.tunes()
            except ModelOpticsError as exc:
                attempt['initial_optics_failure']=exc.diagnostics
                if not opt.model_match_tunes or settings.get('preparation')!='sad_cod':raise
                attempt['initial_tunes']=None
                progress(7,f'Initial SAD COD model unstable; searching model QF1R/QF2R within +/-{bound:.1%} before tune matching (no machine writes)')
            if opt.model_match_tunes:
                attempt['stage']='QF1R / QF2R tune matching'
                progress(10,'Matching model QF1R / QF2R to tune targets (no machine writes)')
                model.match_tunes(settings['tunes'],max_scale=bound,alias=settings['tune_alias'])
            attempt['stage']='checking settings unchanged'
            if not self.backend.same_settings(self.backend.snapshot(),baseline):raise ValueError('Machine settings changed during model preparation')
            verify_inputs(self.backend,settings)
        except Exception as exc:
            attempt.update(status='failed',error=str(exc),exception_type=type(exc).__name__)
            if isinstance(exc,ModelOpticsError):attempt['optics_failure']=exc.diagnostics
            if isinstance(getattr(model,'match_attempt',None),dict):
                attempt['tune_match_attempt']=copy.deepcopy(model.match_attempt)
            if isinstance(exc,ValueError):
                raise ValueError(f"RFTrack model preparation failed during {attempt['stage']}: {exc}. "
                                 "No correction applied. RFTrack model > Save model diagnostics records this attempt.") from exc
            raise
        attempt.update(status='ready',stage='model prepared',tune_match=model.match_report)
        self.model=model;self.settings=settings;self.baseline=baseline
        return model
    def acquire(self,service,mode,opt,progress):
        b=self.backend;model=self.prepare(opt,service.check_cancel,progress)
        # Compute response before any real RF/probe movement; invalid mappings,
        # unstable optics and calibration errors therefore fail read-only.
        virtual=CorrectionService(model);virtual.cancel=service.cancel
        options=copy.deepcopy(opt);options.response_source='measured';options.bpm_samples=1;options.bpm_interval_s=0.
        options.adaptive_probe=False  # Model finite differences need no beam-driven amplitude search.
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
