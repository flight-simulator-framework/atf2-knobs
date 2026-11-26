"""Measured local bump closure and one-dimensional XSR size optimization."""
from dataclasses import dataclass,asdict
import copy
import time
import numpy as np
from .rf_blocks import orbit_slope
from Knobs.DRCorrection.probe_quality import mean_variance, response_quality, wider_probe
from Knobs.DRCorrection.core import CorrectionService,Options,Cancelled
from Knobs.DRCorrection.backends import MagnetCurrentLimitError
from .bump_maps import constrained_step,BumpGeometryUnavailable,BumpRangeUnavailable,BumpLimitUnavailable
from .bump_presets import preset_geometry,bump_targets,bump_preset,ranked_closure_helpers
from .model import name,verify_inputs
from .fresh import AcquisitionUnavailable
from .bump_fit import fit_size,size_fit,next_xsr_point,NoXSRMinimum


@dataclass
class BumpOptions:
    amplitude_mm: float = .4
    refine_mm: float = .1
    calibration_mm: float = .1
    probe_A: float = .05
    max_step_A: float = 2.
    outside_max_mm: float = .030
    outside_rms_mm: float = .020
    position_tolerance_mm: float = .01
    angle_tolerance_mrad: float = .01
    max_iterations: int = 4
    bpm_samples: int = 10
    xsr_samples: int = 10
    settle_s: float = 1.
    use_presets: bool = True  # Legacy serialized option; Bump now always uses presets.

    target_snr: float = 5.

    @property
    def scan_widths_mm(self):
        widths=[2*self.amplitude_mm]
        while widths[-1]>.2:
            widths.append(max(.2,widths[-1]/2))
        return widths

    def validate(self):
        for key,value in asdict(self).items():
            if key=='use_presets':continue
            if not np.isfinite(value) or value<0 or (key!='settle_s' and value==0):
                raise ValueError('Invalid bump option: '+key)
        if self.bpm_samples!=10 or self.xsr_samples!=10:raise ValueError('BPM and XSR acquisition require 10 updates')
        if self.calibration_mm>self.amplitude_mm or self.refine_mm>self.amplitude_mm:
            raise ValueError('Calibration / refinement exceeds scan range')
        if int(self.max_iterations)!=self.max_iterations:raise ValueError('Iteration count must be integer')


class BumpProbeTooWeak(BumpLimitUnavailable):
    pass


class BumpLeakage(BumpRangeUnavailable):
    def __init__(self,observed):
        super().__init__(f"Outside COD exceeds tolerance: max {observed['outside_max_mm']*1000:.2f} µm, "
                         f"RMS {observed['outside_rms_mm']*1000:.2f} µm")
        self.outside=observed['outside']


def restore_correctors(backend,names,values):
    """Try every known original current, even if another restoration fails."""
    errors=[]
    for n,v in zip(names,values):
        try:
            if hasattr(backend,'restore_probe'):backend.restore_probe('correctors',n,float(v))
            else:backend.write('correctors',[n],[float(v)])
        except Exception as exc:errors.append(n+': '+str(exc))
    if errors:raise RuntimeError('Corrector restoration incomplete: '+'; '.join(errors))


class CouplingService:
    def __init__(self,backend,cancel):
        self.backend=backend;self.cancel=cancel;self.reader=CorrectionService(backend);self.reader.cancel=cancel
        self.last_result=None
        self.response_matrix=None
        self.matrix_on_current_beam=False
        self.frozen_bpms={}
        self.xsr_history_floor_um=0.

    def check(self):self.reader.check_cancel()

    def ranked_targets(self):
        self.last_result=None
        if self.backend.unit('skews')!='A':
            raise ValueError('Trim-current ranking needs SD/SF readbacks in A (no physical XSR in native RFTrack)')
        rows=[]
        for n in self.backend.skews:
            if not name(n).startswith(('SD1R.','SF1R.')):continue
            self.check()
            try:
                value=float(self.backend.read('skews',[n])[0])
                if not np.isfinite(value):raise ValueError('nonfinite current')
                rows.append(dict(name=name(n),current_A=value,available=True))
            except Exception as exc:rows.append(dict(name=name(n),current_A=None,available=False,error=str(exc)))
        return sorted(rows,key=lambda r:(not r['available'],-abs(r['current_A'] or 0.)))

    def mask_frozen_bpms(self,frame,event):
        raw=frame['valid']
        if not frame['freshness_verified'] or frame['nshots']!=10:return raw
        x,y=(frame['samples'][axis] for axis in ('x','y'))
        constant=np.all(x==x[0],axis=0)&np.all(y==y[0],axis=0)
        before=set(self.frozen_bpms)
        for i,bpm in enumerate(frame['names']):
            if not raw[i]:continue
            pair=(x[0,i],y[0,i])
            if constant[i]:
                self.frozen_bpms[bpm]=pair
            elif bpm in self.frozen_bpms and pair!=self.frozen_bpms[bpm]:
                del self.frozen_bpms[bpm]
            elif bpm in self.frozen_bpms:
                # The first part of this batch may still be stale. Admit the
                # next fully valid, varying batch even if the first pair recurs.
                self.frozen_bpms[bpm]=None
        active=set(self.frozen_bpms)
        if active!=before:
            event(dict(kind='bpm_health',frozen=sorted(active),recovered=sorted(before-active)))
        frame['frozen_bpms']=sorted(active)
        frame['acquisition_valid']=raw
        frame['valid']=raw & ~np.isin(frame['names'],list(active))
        return frame['valid']

    def orbit(self,opt,on_sample=None,*,required=(),essential=(),outside=(),event=lambda *_:None):
        extra_updates=3
        def retry(attempt,names,accepted,widespread):
            delay=' in 3 s' if widespread else ''
            self.status(event,'bpm_retry',f'BPM retry {attempt}/{extra_updates}{delay}: {names}',
                        samples=accepted,total=opt.bpm_samples)
            if widespread and self.cancel.wait(3.):self.check()
        deadline=None
        while True:
            strict=[i for i in required if self.backend.bpm_names[i] not in self.frozen_bpms]
            for attempt in range(extra_updates+1):
                try:
                    x,y,_=self.reader._orbit(Options(bpm_samples=opt.bpm_samples),on_sample=on_sample,
                        required_bpms=strict,extra_updates=extra_updates,on_retry=retry)
                    break
                except (AcquisitionUnavailable,TimeoutError):
                    if attempt==extra_updates:raise
                retry(attempt+1,'BPM waveform unavailable',0,True)
            frame=self.reader.orbit_measurements[-1]
            valid=self.mask_frozen_bpms(frame,event)
            if (not self.frozen_bpms or (valid.any() and valid[np.asarray(essential,int)].all() and
                    (not len(outside) or np.count_nonzero(valid[np.asarray(outside,int)])>=4))):
                return np.where(np.tile(valid,2),np.r_[x,y],np.nan),valid
            if deadline is None:deadline=time.monotonic()+90.
            if time.monotonic()>=deadline:
                raise ValueError('Frozen BPMs: insufficient live readings for this bump after 90 s')
            self.status(event,'bpm_retry','Waiting for frozen BPMs needed for this measurement')
            if self.cancel.wait(3.):self.check()

    @staticmethod
    def status(event,stage,message,**details):
        event(dict(kind='status',stage=stage,message=message,**details))

    def acquisition_status(self,event,stage,message,**details):
        self.status(event,stage,message,samples=0,total=10,**details)
        return lambda count,total:self.status(event,stage,message,samples=count,total=total,**details)

    def prepare(self,target,opt,model_options=None,progress=lambda *_:None,event=lambda *_:None):
        """Bind saved transport to current readings; no model or tune preparation."""
        opt.validate();self.backend.validate_mode('orbit');self.check();self.last_result=None
        options=model_options or Options(model_match_tunes=False)
        self.status(event,'check',f'1/5 {target}: Checking preset and current settings')
        baseline=self.backend.snapshot();progress(0,'Checking preset BPMs and current budget')
        preset=bump_preset(target)
        internal=set(preset['internal_bpms']) if preset else set()
        internal.difference_update(name(n) for n in options.excluded_bpms)
        required=[i for i,n in enumerate(self.backend.bpm_names) if name(n) in internal]
        _,valid=self.orbit(opt,self.acquisition_status(event,'check',f'1/5 {target}: Checking BPM validity'),
                           required=required,essential=required,event=event)
        valid=self.reader.orbit_measurements[-1].get('acquisition_valid',valid)
        try:geometry=preset_geometry(self.backend,target,valid,opt,options)
        except BumpLimitUnavailable as exc:
            self.last_result=dict(target=target,stage='preset check',baseline=baseline,
                                  bpm_names=list(self.backend.bpm_names),actuators=[],centers=[],
                                  restored=True,magnet_writes=False,error=str(exc))
            raise
        if not self.backend.same_settings(self.backend.snapshot(),baseline):
            raise ValueError('Machine settings changed during preset check; calibrate again')
        geometry['baseline']=baseline;geometry['options']=asdict(opt)
        progress(100,'Saved bump preset checked; no magnet changed')
        return geometry

    def calibrate_target(self,target,opt,options=None,progress=lambda *_:None,event=lambda *_:None):
        geometry=self.prepare(target,opt,options,lambda p,s:progress(round(p*.1),s),event)
        return self.calibrate_with_helpers(geometry,opt,progress,event)

    def extend_geometry(self,g,opt,loss,event):
        if len(g.get('closure_helpers',()))>=2:return None
        self.verify_geometry(g)
        measured=self.response_matrix['columns'] if self.response_matrix is not None else None
        for n in ranked_closure_helpers(self.backend,g,loss.outside,opt,measured):
            self.check()
            center=float(self.backend.read('correctors',[n])[0])
            try:low,high=np.asarray(self.backend.limits('correctors',[n]),float)[:,0]
            except MagnetCurrentLimitError:continue
            if not np.isfinite([center,low,high]).all() or min(center-low,high-center)<=1.e-6:continue
            geometry={key:copy.deepcopy(g[key]) for key in (
                'target','actuators','bpm_names','positions_m','outside_indices','outside_rows','internal_indices',
                'internal_bpms','reconstruction','centers','limits','baseline','options','selection','fixed_actuators',
                'model_settings','main_currents_A','source','predicted_range_mm','target_position_note') if key in g}
            geometry['actuators']=[*g['actuators'],n]
            geometry['centers']=np.r_[g['centers'],center]
            geometry['limits']=[np.r_[g['limits'][0],low],np.r_[g['limits'][1],high]]
            geometry['closure_helpers']=[*g.get('closure_helpers',()),n]
            if 'response' in g:
                geometry['previous_columns']=dict(names=list(g['actuators']),response=g['response'],
                                                  readbacks=g['probe_readbacks'])
            self.status(event,'helper',f"{g['target']}: adding closure corrector {n}")
            return geometry
        return None

    def calibrate_with_helpers(self,geometry,opt,progress,event):
        while True:
            event(dict(kind='geometry',data=copy.deepcopy(geometry)))
            try:return self.calibrate(geometry,opt,progress,event)
            except BumpLeakage as loss:
                geometry=self.extend_geometry(self.last_result,opt,loss,event)
                if geometry is None:raise

    def available_targets(self,opt,model_options=None,progress=lambda *_:None,event=lambda *_:None):
        """Read-only preset checks; no lattice construction or tune matching."""
        opt.validate();self.backend.validate_mode('orbit');self.check();self.last_result=None
        options=model_options or Options(model_match_tunes=False)
        baseline=self.backend.snapshot();_,valid=self.orbit(opt,event=event);targets=bump_targets();results=[]
        valid=self.reader.orbit_measurements[-1].get('acquisition_valid',valid)
        for i,target in enumerate(targets):
            self.check();progress(round(99*i/len(targets)),'Checking preset BPMs / current budget: '+target)
            try:
                g=preset_geometry(self.backend,target,valid,opt,options)
                results.append(dict(target=target,available=True,internal_bpms=g['internal_bpms'],
                    actuators=g['actuators'],selection=g['selection'],predicted_range_mm=g['predicted_range_mm']))
            except BumpGeometryUnavailable as exc:
                results.append(dict(target=target,available=False,reason=str(exc)))
        if not self.backend.same_settings(self.backend.snapshot(),baseline):
            raise ValueError('Machine settings changed during position check; check again')
        progress(100,'Bump presets checked; no magnet changed')
        return dict(rows=results,baseline=baseline,options=asdict(opt),checked_unix_s=time.time(),
                    model_match_tunes=False,source='Saved nominal bump presets; no runtime RFTrack model')

    def verify_geometry(self,geometry):
        if list(self.backend.bpm_names)!=list(geometry['bpm_names']):raise ValueError('BPM mapping changed')
        if not self.backend.same_settings(self.backend.snapshot(),geometry['baseline']):
            raise ValueError('Settings changed since calibration; calibrate again')
        if 'main_currents_A' in geometry:
            current=self.backend.bump_optics_currents()
            tolerance=getattr(self.backend,'profile',{}).get('current_tolerance_A',1.e-5)
            saved=geometry['main_currents_A']
            if current.keys()!=saved.keys() or any(not np.isfinite(current[n]) or abs(current[n]-v)>tolerance for n,v in saved.items()):
                raise ValueError('Main optics changed since current-beam bump calibration')
        if geometry.get('model_settings'):verify_inputs(self.backend,geometry['model_settings'])

    def move(self,names,values,opt):
        self.check()
        try:self.backend.write('correctors',names,values)
        except MagnetCurrentLimitError as exc:raise BumpLimitUnavailable(str(exc)) from exc
        actual=self.backend.read('correctors',names)
        tolerance=getattr(self.backend,'profile',{}).get('current_tolerance_A',1.e-5)
        if not np.isfinite(actual).all() or np.max(abs(actual-values))>tolerance:
            raise RuntimeError('Bump corrector readback mismatch')
        if self.cancel.wait(opt.settle_s):self.check()
        return actual

    def observe(self,g,baseline,opt,on_sample=None,*,event=lambda *_:None,at_baseline=False):
        required=np.unique(np.r_[g['outside_indices'],g['internal_indices']])
        outside_indices=np.asarray(g['outside_indices'])
        n=len(g['bpm_names'])
        referenced=np.isfinite(baseline[:n])&np.isfinite(baseline[n:])
        eligible=outside_indices if at_baseline else outside_indices[referenced[outside_indices]]
        if 'response' in g:
            reliable=np.isfinite(g['response']).reshape(2,n,-1).all(axis=(0,2))
            eligible=eligible[reliable[eligible]]
        if len(eligible)<4:raise ValueError('Need four outside BPMs with baseline and response; recalibrate the bump')
        orbit,valid=self.orbit(opt,on_sample,required=required,essential=g['internal_indices'],outside=eligible,event=event)
        if at_baseline:
            missing=~np.isfinite(baseline)&np.tile(valid,2)
            baseline[missing]=orbit[missing]
        delta=orbit-baseline
        state=np.asarray(g['reconstruction'])@np.nan_to_num(delta,nan=0.)
        outside=delta[np.asarray(g['outside_rows'],int)]
        planes=outside.reshape(2,-1)
        planes=planes[:,np.isfinite(planes).all(axis=0)]
        if planes.shape[1]<4:raise ValueError('Need four outside BPMs with live readings and a measured baseline')
        rms=np.sqrt(np.mean(planes**2,axis=1))
        return dict(orbit_mm=orbit,valid=valid,state=state,outside_max_mm=float(np.max(abs(planes))),
                    outside_rms_mm=float(max(rms)),outside_rms_xy_mm=rms,
                    outside_max_xy_mm=np.max(abs(planes),axis=1),outside=outside)

    @staticmethod
    def closed(observation,amplitude,opt):
        state=observation['state'];desired=np.array([0,0,amplitude,0.])
        tolerance=np.array([opt.position_tolerance_mm,opt.angle_tolerance_mrad]*2)
        return (observation['outside_max_mm']<=opt.outside_max_mm and
                observation['outside_rms_mm']<=opt.outside_rms_mm and np.all(abs(state-desired)<=tolerance))

    def close_at(self,g,response,knob,baseline,amplitude,opt,event):
        names=g['actuators'];centers=np.asarray(g['centers']);lower,upper=np.asarray(g['limits'])
        lower=np.maximum(lower,centers-opt.max_step_A);upper=np.minimum(upper,centers+opt.max_step_A)
        for seed in (knob,*g.get('prior_scan_knobs',())):
            command=centers+amplitude*seed
            if np.all(command>=lower-1.e-9) and np.all(command<=upper+1.e-9):break
        else:raise BumpLimitUnavailable('Scaled bump exceeds current / step bounds')
        command=np.clip(command,lower,upper)  # Roundoff only, after the strict physical-bound check.
        constraint=np.asarray(g['reconstruction'])@np.nan_to_num(response,nan=0.);desired=np.array([0.,0.,amplitude,0.])
        for iteration in range(opt.max_iterations+1):
            label=f"{g['target']}: Y {amplitude:+.3f} mm / "+('Initial setting' if iteration==0 else f'Closure correction {iteration}/{opt.max_iterations}')
            details=dict(magnets=list(names),command_A=command.copy(),baseline_A=centers.copy(),amplitude_mm=float(amplitude),iteration=iteration)
            self.status(event,'closure',label+f' — Setting currents; settling {opt.settle_s:g} s',**details)
            currents=self.move(names,command,opt)
            observed=self.observe(g,baseline,opt,self.acquisition_status(event,'closure',label+' — Measuring outside COD and target position',readback_A=currents.copy(),**details),event=event,at_baseline=np.array_equal(command,centers))
            observed.update(amplitude_mm=float(amplitude),iteration=iteration,currents_A=currents.copy(),closed=self.closed(observed,amplitude,opt))
            event(dict(kind='orbit',**observed))
            if self.closed(observed,amplitude,opt):return observed
            if iteration==opt.max_iterations:break
            outside_response=response[np.asarray(g['outside_rows'],int)]
            usable=np.isfinite(outside_response).all(axis=1)&np.isfinite(observed['outside'])
            if np.count_nonzero(usable)<8:raise ValueError('Insufficient measured outside response for correction')
            try:
                step=constrained_step(outside_response[usable],-observed['outside'][usable],constraint,
                                      desired-observed['state'],lower-command,upper-command)
            except ValueError as exc:
                if observed['outside_max_mm']>opt.outside_max_mm or observed['outside_rms_mm']>opt.outside_rms_mm:
                    raise BumpLeakage(observed) from exc
                raise
            command=command+step
        if observed['outside_max_mm']>opt.outside_max_mm or observed['outside_rms_mm']>opt.outside_rms_mm:
            raise BumpLeakage(observed)
        raise BumpRangeUnavailable('Beam-based bump did not close within COD / target tolerances')

    def probe_column(self,n,center,span,opt,required,j,total,event,*,essential=(),outside=(),quality_bpms=None):
        while True:
            column,record=self._probe_pair(n,center,span,opt,required,j,total,event,essential=essential,outside=outside,quality_bpms=quality_bpms)
            quality=record['quality']
            if quality['usable']:return column,record
            low,high=np.asarray(self.backend.limits('correctors',[n]),float)[:,0]
            larger=wider_probe(span,quality,min(opt.max_step_A,center-low,high-center))
            if larger<=span+1.e-9:
                raise BumpProbeTooWeak(f'{n}: orbit response below S/N {opt.target_snr:g} at ±{span:g} A')
            self.status(event,'probe',f'{n}: low S/N, retry at ±{larger:.4g} A')
            span=larger

    def _probe_pair(self,n,center,span,opt,required,j,total,event,*,essential,outside,quality_bpms):
        values=[];readbacks=[];variances=[]
        outside=np.asarray(outside,int)
        try:
            for sign in (-1,1):
                command=center+sign*span
                details=dict(magnets=[n],command_A=[float(command)],baseline_A=[float(center)],magnet_index=j+1,magnet_count=total)
                label=f'2/5 Response {j+1}/{total}: {n} / delta {sign*span:+.4f} A'
                self.status(event,'probe',label+f' — Setting currents; settling {opt.settle_s:g} s',**details)
                readbacks.append(float(self.move([n],np.array([command]),opt)[0]))
                orbit,mask=self.orbit(opt,self.acquisition_status(event,'probe',label+' — Acquiring BPM updates',readback_A=[readbacks[-1]],**details),required=required,essential=essential,outside=outside,event=event)
                values.append(np.where(np.tile(mask,2),orbit,np.nan))
                frame=self.reader.orbit_measurements[-1]
                variances.append(np.concatenate([mean_variance(frame,axis) for axis in ('x','y')]))
                outside=outside[mask[outside]]
        finally:
            self.status(event,'restore_probe',f'2/5 {n}: Sending restore command {center:+.4f} A')
            restore_correctors(self.backend,[n],[center])
        measured_span=readbacks[1]-readbacks[0]
        if measured_span<=max(1.e-6,span*.2):raise ValueError('No resolved corrector current probe: '+n)
        change=values[1]-values[0]
        use=np.isfinite(change)
        if quality_bpms is not None:use &= np.tile(quality_bpms,2)
        if len(required):use &= np.tile(np.isin(np.arange(len(use)//2),required),2)
        if not use.any():raise ValueError(f'{n}: no common valid BPM for orbit response')
        quality=response_quality(np.sqrt(np.mean(change[use]**2)),np.sqrt(np.mean((variances[0]+variances[1])[use])),opt.target_snr)
        return orbit_slope(values[0],values[1],readbacks), dict(magnet=n,requested=[center-span,center+span],actual=readbacks,quality=quality)

    def check_matrix_settings(self,matrix,baseline):
        if (matrix.get('schema')!='dr-bump-response-1' or matrix.get('machine')!=self.backend.identity
                or matrix.get('bpm_names')!=list(self.backend.bpm_names)):
            raise ValueError('Bump response matrix machine or BPM mapping differs')
        # Each successful bump intentionally changes steering currents.
        saved=matrix['baseline']
        optics=self.backend.bump_optics_currents() if hasattr(self.backend,'bump_optics_currents') else {}
        previous=matrix.get('main_currents_A',{})
        tolerance=getattr(self.backend,'profile',{}).get('current_tolerance_A',1.e-5)
        if (optics.keys()!=previous.keys() or not np.isfinite(list(previous.values())).all()
                or any(abs(optics[k]-previous[k])>tolerance for k in optics)
                or not self.backend.same_settings(baseline,dict(saved,correctors=baseline['correctors']))):
            raise ValueError('Optics or RF settings changed since response measurement. Remeasure the shared matrix or use per-target probes.')

    def validate_response_matrix(self,matrix):
        if (matrix.get('schema')!='dr-bump-response-1' or matrix.get('machine')!=self.backend.identity
                or matrix.get('bpm_names')!=list(self.backend.bpm_names) or self.backend.unit('correctors')!='A'):
            raise ValueError('Bump response matrix machine, units or BPM mapping differs')
        size=2*len(self.backend.bpm_names)
        magnets=set(self.backend.hcorrectors+self.backend.vcorrectors)
        if not matrix.get('columns') or not np.isfinite(matrix['measured_unix_s']):
            raise ValueError('Empty or incomplete bump response matrix')
        for n,entry in matrix['columns'].items():
            column=np.asarray(entry['response_mm_per_A'],float)
            actual=np.asarray(entry['readback']['actual'],float)
            if (n not in magnets or entry['readback']['magnet']!=n or column.shape!=(size,)
                    or np.isinf(column).any() or actual.shape!=(2,) or not np.isfinite(actual).all() or actual[1]<=actual[0]):
                raise ValueError('Invalid measured response column: '+n)
    def use_response_matrix(self,matrix,*,current_beam=False):
        self.validate_response_matrix(matrix)
        if not current_beam:self.check_matrix_settings(matrix,self.backend.snapshot())
        self.response_matrix=matrix
        self.matrix_on_current_beam=current_beam

    def measure_response_matrix(self,opt,options,state,progress,event):
        opt.validate();self.backend.validate_mode('orbit');self.check()
        if self.backend.unit('correctors')!='A':raise ValueError('Shared bump measurements require corrector currents in A')
        baseline=self.backend.snapshot()
        if state.get('settings_changed'):state.clear()
        if not state:
            optics=self.backend.bump_optics_currents() if hasattr(self.backend,'bump_optics_currents') else {}
            state.update(schema='dr-bump-response-1',machine=self.backend.identity,bpm_names=list(self.backend.bpm_names),
                         baseline=baseline,main_currents_A=optics,columns={},skipped={},probe_A=opt.probe_A)
        elif not self.backend.same_settings(baseline,state['baseline']):
            raise ValueError('Settings changed since the interrupted response measurement')
        else:self.check_matrix_settings(state,baseline)
        self.last_result=state
        fixed={name(n) for n in options.fixed_actuators}
        magnets=[n for n in self.backend.hcorrectors+self.backend.vcorrectors if name(n) not in fixed]
        tolerance=getattr(self.backend,'profile',{}).get('current_tolerance_A',1.e-5)
        selected_bpms=~np.isin(self.backend.bpm_names,options.excluded_bpms)
        if not selected_bpms.any():raise ValueError('No enabled BPM for shared response')
        for j,n in enumerate(magnets):
            self.check()
            if n in state['columns']:continue
            center=float(baseline['correctors'][n])
            low,high=np.asarray(self.backend.limits('correctors',[n]),float)[:,0]
            span=min(opt.probe_A,opt.max_step_A,center-low,high-center)
            if not np.isfinite([center,low,high]).all():raise ValueError('Invalid corrector current or limits: '+n)
            if span<=1.e-6:
                state['skipped'][n]='No symmetric current margin'
                continue
            actual=float(self.backend.read('correctors',[n])[0])
            if not np.isfinite(actual) or abs(actual-center)>tolerance:
                state['settings_changed']=True
                raise ValueError('Corrector baseline changed before probing '+n)
            try:
                column,rb=self.probe_column(n,center,span,opt,(),j,len(magnets),event,quality_bpms=selected_bpms)
            except BumpProbeTooWeak as exc:
                state['skipped'][n]=str(exc)
                continue
            finally:
                # Restore only the probed supply. Retain its identity if recovery is needed.
                try:
                    actual=float(self.backend.read('correctors',[n])[0])
                    if not np.isfinite(actual) or abs(actual-center)>tolerance:raise ValueError('Restore readback mismatch: '+n)
                except Exception as exc:
                    state.update(actuators=[n],centers=[center],restoration_error=str(exc))
                    raise
                self.reader.orbit_measurements.clear()
            state['skipped'].pop(n,None)
            state['columns'][n]=dict(response_mm_per_A=column,readback=rb)
            progress(round(99*(j+1)/len(magnets)),f'Measured {n} ({j+1}/{len(magnets)})')
        final=self.backend.snapshot()
        try:
            self.check_matrix_settings(state,final)
            if not self.backend.same_settings(final,baseline):
                raise ValueError('Machine settings changed during the shared response measurement')
        except ValueError:
            state['settings_changed']=True
            raise
        if not state['columns']:raise ValueError('No eligible corrector columns measured')
        state['measured_unix_s']=time.time()
        return state

    def calibrate(self,geometry,opt,progress=lambda *_:None,event=lambda *_:None):
        opt.validate();self.backend.validate_mode('orbit');self.verify_geometry(geometry);g=copy.deepcopy(geometry);names=g['actuators']
        self.last_result=g
        centers=np.asarray(g['centers']);lower,upper=np.asarray(g['limits'])
        required=np.unique(np.r_[g['outside_indices'],g['internal_indices']])
        if self.response_matrix is not None and self.matrix_on_current_beam and hasattr(self.backend,'bump_optics_currents'):
            g['main_currents_A']=self.backend.bump_optics_currents()
            if not np.isfinite(list(g['main_currents_A'].values())).all():raise ValueError('Invalid current bump optics')
        baseline,baseline_valid=self.orbit(opt,self.acquisition_status(event,'baseline',f"1/5 {g['target']}: Measuring baseline orbit"),required=required,essential=g['internal_indices'],outside=g['outside_indices'],event=event)
        columns=[];g['probe_readbacks']=[];self.reader.orbit_measurements=[]
        previous=g.pop('previous_columns',None)
        if previous:
            if list(names[:len(previous['names'])])!=previous['names']:
                raise ValueError('Added corrector differs from the measured response ordering')
            columns=list(np.asarray(previous['response']).T)
            g['probe_readbacks']=previous['readbacks']
        cached={}
        g['shared_columns']=[r['magnet'] for r in g['probe_readbacks'] if r.get('source')=='shared matrix']
        if self.response_matrix is not None:
            if not self.matrix_on_current_beam:self.check_matrix_settings(self.response_matrix,g['baseline'])
            cached=self.response_matrix['columns']
        spans=np.minimum(min(opt.probe_A,opt.max_step_A),np.minimum(centers-lower,upper-centers))
        if (spans<=1.e-6).any():
            g.update(restored=True,calibrated=False)
            raise BumpLimitUnavailable('No symmetric probe margin for selected correctors')
        outside=np.asarray(g['outside_indices'])
        common=outside[baseline_valid[outside]]
        n_bpms=len(baseline_valid)
        for column in columns:
            common=common[np.isfinite(column.reshape(2,n_bpms)[:,common]).all(axis=0)]
        required_rows=np.r_[required[baseline_valid[required]],required[baseline_valid[required]]+n_bpms]
        try:
            for j in range(len(columns),len(names)):
                n,center,span=names[j],centers[j],spans[j]
                entry=cached.get(n)
                if entry is not None:
                    column=np.asarray(entry['response_mm_per_A'],float)
                    q=entry['readback'].get('quality')
                    if not np.isfinite(column[required_rows]).all() or (q and q['signal_rms']<opt.target_snr*q['noise_rms']):entry=None
                if entry is None:
                    column,readback=self.probe_column(n,center,span,opt,required,j,len(names),event,
                                                     essential=g['internal_indices'],outside=common)
                else:
                    readback=dict(entry['readback'],source='shared matrix',measured_unix_s=self.response_matrix['measured_unix_s'])
                    g['shared_columns'].append(n)
                columns.append(column)
                common=common[np.isfinite(column.reshape(2,n_bpms)[:,common]).all(axis=0)]
                g['probe_readbacks'].append(readback)
                progress(round(70*(j+1)/len(names)),f"Bump response {j+1}/{len(names)}: {n} ({'shared' if entry else 'measured'})")
            response=np.column_stack(columns);g['response']=response
            self.status(event,'solve','3/5 Solving Y bump currents from measured response; constraining X, angles and outside COD')
            progress(72,'Calculating Y bump currents from measured response')
            margin=np.minimum(opt.max_step_A,np.minimum(centers-lower,upper-centers))/opt.calibration_mm
            outrows=np.asarray(g['outside_rows'],int)
            usable=np.isfinite(response[outrows]).all(axis=1)&np.isfinite(baseline[outrows])
            if np.count_nonzero(usable)<8:raise ValueError('Need four outside BPMs with measured baseline and response')
            try:
                knob=constrained_step(response[outrows[usable]],np.zeros(np.count_nonzero(usable)),
                                      np.asarray(g['reconstruction'])@np.nan_to_num(response,nan=0.),[0,0,1,0],-margin,margin)
            except BumpLimitUnavailable as exc:
                raise BumpLimitUnavailable('Measured response cannot form the calibration bump: '+str(exc)) from exc
            checks=[]
            for amplitude in (opt.calibration_mm,-opt.calibration_mm):
                self.status(event,'closure',f'4/5 Checking closure at Y {amplitude:+.3f} mm')
                progress(76 if amplitude>0 else 86,f'Closing calibration bump Y {amplitude:+.3f} mm')
                observed=self.close_at(g,response,knob,baseline,amplitude,opt,event);checks.append(observed)
                # Keep a common linear knob. Scan points still verify actual closure.
                if amplitude>0:knob=(observed['currents_A']-centers)/amplitude
            knob=(checks[0]['currents_A']-checks[1]['currents_A'])/(2*opt.calibration_mm)
            if np.any(abs(knob)>margin+1.e-10):raise BumpLimitUnavailable('Calibrated bump exceeds the calibration current margin')
            g.update(response=response,knob_A_per_mm=knob,reference_orbit_mm=baseline,calibration_checks=checks,
                     calibrated=False,options=asdict(opt),orbit_measurements=copy.deepcopy(self.reader.orbit_measurements))
        except BaseException as exc:
            g.update(error=str(exc),calibrated=False)
            raise
        finally:
            g['orbit_measurements']=copy.deepcopy(self.reader.orbit_measurements)
            self.status(event,'restore','5/5 Sending baseline-current restore commands to all correctors',magnets=list(names),command_A=centers.copy(),baseline_A=centers.copy())
            try:restore_correctors(self.backend,names,centers);g['restored']=True
            except BaseException as exc:
                g.update(restoration_error=str(exc),restored=False,calibrated=False)
                self.status(event,'restore_failed','Restoration failed: '+str(exc))
                raise
        self.status(event,'restore_verify','5/5 Restore commands sent — Verifying baseline readbacks')
        self.check();self.verify_geometry(g)
        if not baseline_valid[np.asarray(g['outside_indices'])].all():
            orbit,valid=self.orbit(opt,required=required,essential=g['internal_indices'],event=event)
            missing=~np.isfinite(baseline)&np.tile(valid,2)
            baseline[missing]=orbit[missing]
        g['calibrated']=True;self.last_result=g
        self.status(event,'complete',f"{g['target']}: Calibration complete; baseline currents verified. Ready to scan.")
        progress(100,'Measured bump calibrated in both signs; baseline currents restored')
        return g

    def select_scan_range(self,g,opt,result,progress=lambda *_:None,event=lambda *_:None):
        """Test contiguous windows around zero, prioritizing width over centering.

        The default widths are 0.8, 0.4 and 0.2 mm, in that order. Window
        centers are tested nearest zero first in 1/8-width increments. Every
        window contains the baseline (zero); a known failed interior point
        invalidates a window. These are COD checks only, with no XSR acquisition.
        Actual XSR measurements must independently pass closure again.
        """
        baseline=np.asarray(g['reference_orbit_mm']);knob=np.asarray(g['knob_A_per_mm'])
        response=np.asarray(g['response']);checked={}
        result['range_checks']=[];result['range_attempts']=[]
        currents=np.asarray(g['centers']);lower,upper=np.asarray(g['limits'])
        lower=np.maximum(lower,currents-opt.max_step_A)-currents
        upper=np.minimum(upper,currents+opt.max_step_A)-currents
        active=abs(knob)>1.e-12
        endpoints=np.array([lower[active]/knob[active],upper[active]/knob[active]])
        reachable_low=float(np.max(np.min(endpoints,axis=0))) if active.any() else 0.
        reachable_high=float(np.min(np.max(endpoints,axis=0))) if active.any() else 0.
        for width in opt.scan_widths_mm:
            centers=[0.]+[sign*width*i/8 for i in range(1,5) for sign in (1,-1)]
            # Do not miss a full-width window merely because a supply bound
            # falls between the regular center steps (e.g. [-0.35,+0.45]).
            low_min=max(reachable_low,-width);low_max=min(reachable_high-width,0.)
            if low_min<=low_max:
                aligned=float(np.clip(-width/2,low_min,low_max))+width/2
                if not any(abs(aligned-c)<1.e-10 for c in centers):centers.append(aligned)
            for center in centers:
                self.check();low,high=np.round([center-width/2,center+width/2],12)
                attempt=dict(bounds_mm=[float(low),float(high)],width_mm=width,status='checking')
                result['range_attempts'].append(attempt)
                failed=[a for a,entry in checked.items() if not entry['success'] and low-1.e-12<=a<=high+1.e-12]
                if failed:
                    attempt.update(status='rejected',reason='Contains a displacement with failed closure',failed_mm=failed)
                    continue
                progress(0,f'Checking bump scan range {low:+.3f} to {high:+.3f} mm (width {width:.3f} mm)')
                # Endpoints first, then midpoint and quarters. Reuse only the
                # range-search evidence; coarse/refinement/final measurements
                # always move and recheck COD before their XSR reads.
                grid=np.round(np.linspace(low,high,5),12)
                for index in (0,4,2,1,3):
                    amplitude=float(grid[index])
                    if amplitude not in checked:
                        entry=dict(amplitude_mm=amplitude,success=False)
                        result['range_checks'].append(entry)
                        try:
                            observed=self.close_at(g,response,knob,baseline,amplitude,opt,event)
                            entry.update(success=True,observation=observed)
                        except BumpRangeUnavailable as exc:
                            entry['error']=str(exc)
                            if isinstance(exc,BumpLeakage) and len(g.get('closure_helpers',()))==2:raise
                            restore_correctors(self.backend,g['actuators'],g['centers'])
                            if isinstance(exc,BumpLeakage):
                                expanded=self.extend_geometry(g,opt,exc,event)
                                if expanded is not None:
                                    exc.expanded=expanded
                                    raise
                        checked[amplitude]=entry
                    entry=checked[amplitude]
                    if not entry['success']:
                        attempt.update(status='rejected',reason=entry['error'])
                        attempt.setdefault('failed_mm',[]).append(amplitude)
                    # Check both ends before shifting outward: if both +/-0.4
                    # fail, every contiguous 0.8 mm window containing zero is
                    # already ruled out. Do not needlessly try +/-0.5 or beyond.
                    if index!=0 and attempt['status']=='rejected':break
                else:
                    attempt['status']='selected'
                    selected=dict(bounds_mm=[float(low),float(high)],width_mm=width,center_mm=center,
                        requested_width_mm=2*opt.amplitude_mm,fallback=bool(width<2*opt.amplitude_mm),
                        shifted=bool(abs(center)>1.e-12),center_step_mm=width/8)
                    result['scan_range']=selected
                    event(dict(kind='range',scan_range=selected))
                    progress(5,f'Selected bump scan range {low:+.3f} to {high:+.3f} mm (width {width:.3f} mm)')
                    return selected
        raise BumpLimitUnavailable(f'No closed scan window down to {opt.scan_widths_mm[-1]:g} mm full width')

    def scan(self,calibration,opt,progress=lambda *_:None,event=lambda *_:None,*,resume=None):
        if resume is not None:
            if resume.get('applied') or not resume.get('restored') or resume['calibration']['target']!=calibration['target']:
                raise ValueError('Cannot resume this bump scan from its recorded baseline')
            calibration=resume['calibration']
        while True:
            if resume and 'pending_geometry' in resume:
                geometry=resume['pending_geometry']
                self.last_result=resume
                try:
                    updated=self.calibrate_with_helpers(geometry,opt,progress,event)
                except BaseException as exc:
                    failure=self.last_result
                    if failure is not resume and failure and 'actuators' in failure:
                        if failure['actuators']!=geometry['actuators']:resume['pending_geometry']=failure
                        if failure.get('restoration_error'):
                            resume.update(restored=False,restoration_error=failure['restoration_error'])
                    resume['error']=str(exc)
                    self.last_result=resume
                    raise
                # The fit spans both calibrations. Keep the displacement zero
                # used for the first XSR points, with the same steering centers.
                original=np.asarray(calibration['reference_orbit_mm'])
                updated['reference_orbit_mm']=np.where(np.isfinite(original),original,updated['reference_orbit_mm'])
                # An enlarged knob can hit a current limit sooner. The earlier
                # knob remains a feasible starting command with new helpers at
                # baseline; close_at still verifies and corrects on beam.
                added=len(updated['actuators'])-len(calibration['actuators'])
                updated['prior_scan_knobs']=[np.pad(seed,(0,added)) for seed in
                    (calibration['knob_A_per_mm'],*calibration.get('prior_scan_knobs',()))]
                calibration=updated
                resume['calibration']=updated
                del resume['pending_geometry']
                self.last_result=resume
            try:return self.scan_calibrated(calibration,opt,progress,event,resume)
            except BumpLeakage as loss:
                resume=self.last_result
                calibration=resume['calibration']
                geometry=getattr(loss,'expanded',None) or self.extend_geometry(calibration,opt,loss,event)
                if geometry is None:raise
                resume['pending_geometry']=geometry
                self.status(event,'helper',f"{geometry['target']}: keeping {len(resume['points'])} completed XSR points")

    def scan_calibrated(self,calibration,opt,progress,event,resume):
        opt.validate()
        if not hasattr(self.backend,'sample_xsr'):raise ValueError('Backend has no measured XSR size acquisition')
        if not calibration.get('calibrated'):raise ValueError('Calibrate the local bump first')
        if calibration['options']!=asdict(opt):raise ValueError('Bump options changed; calibrate again')
        self.backend.validate_mode('orbit');self.verify_geometry(calibration);self.check()
        g=copy.deepcopy(calibration);baseline=np.asarray(g['reference_orbit_mm']);knob=np.asarray(g['knob_A_per_mm'])
        response=np.asarray(g['response']);self.reader.orbit_measurements=[]
        result=dict(schema='dr-coupling-bump-1',calibration=g,options=asdict(opt),
                    points=[],fits=[],history_floor_um=resume.get('history_floor_um',0.) if resume else self.xsr_history_floor_um,
                    started_unix_s=resume['started_unix_s'] if resume else time.time(),applied=False)
        self.last_result=result
        retained=resume['points'][:resume.get('resume_from')] if resume else []
        if retained and retained[-1]['phase']=='final verification':retained=retained[:-1]
        if resume:
            event(dict(kind='scan_history',target=g['target'],points=retained,
                       fits=[f for f in resume['fits'] if f.get('point_count',5)<=len(retained)]))
        saved_points=iter(retained)
        def measure(amplitude,phase):
            previous=next(saved_points,None)
            if previous is not None and phase!='final verification':
                if previous['phase']!=phase or not np.isclose(previous['requested_amplitude_mm'],amplitude):
                    raise ValueError('Saved XSR point differs from resumed scan grid')
                result['points'].append(previous)
                return previous
            observed=self.close_at(g,response,knob,baseline,float(amplitude),opt,event)
            actual_amplitude=float(observed['state'][2])
            def sample(value):event(dict(kind='sample',amplitude_mm=actual_amplitude,requested_amplitude_mm=float(amplitude),phase=phase,xsr=value))
            xsr=self.backend.sample_xsr(self.cancel,count=10,callback=sample)
            self.check()
            if xsr['count']!=10 or not xsr.get('update_verified'):raise ValueError('Ten fresh XSR updates required')
            point=dict(amplitude_mm=actual_amplitude,requested_amplitude_mm=float(amplitude),phase=phase,xsr=xsr,
                       orbit=observed,actuators=list(g['actuators']))
            result['points'].append(point);event(dict(kind='point',point=point))
            return point
        try:
            if resume and 'scan_range' in resume:
                for key in ('range_checks','range_attempts','scan_range'):
                    result[key]=resume[key]
                selected=result['scan_range']
                event(dict(kind='range',scan_range=selected))
            else:selected=self.select_scan_range(g,opt,result,progress,event)
            low,high=selected['bounds_mm']
            for i,a in enumerate(np.linspace(low,high,5)):
                measure(a,'coarse');progress(10+10*i,f'XSR scan {a:+.3f} mm: 10 updates')
            result['observed_coarse_range_mm']=[min(p['amplitude_mm'] for p in result['points']),max(p['amplitude_mm'] for p in result['points'])]
            result['sem_floor_um']=max(result['history_floor_um'],float(np.median([p['xsr']['sem'] for p in result['points']])))
            result['refinement_mm']=[];result['xsr_bounds_mm']=[float(low),float(high)]
            blocked=[c['amplitude_mm'] for c in result['range_checks'] if not c['success']]
            # Replay any failed extension at the same decision stage on Resume.
            rejections=resume.get('edge_rejections',[]) if resume else []
            result['edge_rejections']=[]
            while True:
                stage=len(result['points'])
                if not result['fits'] or result['fits'][-1]['point_count']!=stage:
                    try:fit=size_fit(result['points'],result['xsr_bounds_mm'],result['sem_floor_um'])
                    except ValueError:
                        result['resume_from']=0 if stage==5 else stage-1
                        raise
                    result['fits'].append(fit)
                    event(dict(kind='fit',fit=fit,index=len(result['fits'])-1))
                    for rejection in rejections:
                        if rejection['stage']==stage:
                            result['edge_rejections'].append(rejection)
                            blocked.append(rejection['amplitude_mm'])
                decision=next_xsr_point(fit,result['points'],selected,opt.refine_mm,blocked)
                if decision is None:break
                amplitude,phase=decision
                self.status(event,'scan',f'{g["target"]}: {phase} at {amplitude:+.3f} mm')
                try:measure(amplitude,phase)
                except BumpLeakage:raise
                except BumpRangeUnavailable as exc:
                    if phase!='edge':raise
                    restore_correctors(self.backend,g['actuators'],g['centers'])
                    result['edge_rejections'].append(dict(stage=stage,amplitude_mm=amplitude,reason=str(exc)))
                    blocked.append(amplitude)
                    continue
                result['refinement_mm'].append(amplitude)
                if phase=='edge':
                    result['xsr_bounds_mm']=[(low+high)/2-.5,(low+high)/2+.5]
                    event(dict(kind='xsr_bounds',bounds=result['xsr_bounds_mm']))
            final=measure(fit['best_mm'],'final verification')
            best_measured=min(result['points'][:-1],key=lambda p:p['xsr']['mean'])
            tolerance=3*np.hypot(max(result['sem_floor_um'],final['xsr']['sem']),
                                 max(result['sem_floor_um'],best_measured['xsr']['sem']))+1.e-3
            if final['xsr']['mean']>best_measured['xsr']['mean']+tolerance:
                raise ValueError('Verified fitted optimum is worse than the best measured size; baseline restored')
            result.update(applied=True,final_amplitude_mm=fit['best_mm'],final=final,
                          final_observed_amplitude_mm=final['amplitude_mm'],
                          final_currents_A=self.backend.read('correctors',g['actuators']))
            self.check();progress(100,f"Set fitted optimum {fit['best_mm']:+.4f} mm; final XSR verified")
        except NoXSRMinimum as exc:
            result.update(skip_reason=str(exc),applied=False)
            try:
                restore_correctors(self.backend,g['actuators'],g['centers'])
                self.verify_geometry(g);result['restored']=True
            except Exception as restore_error:
                result.update(restoration_error=str(restore_error),restored=False)
                raise
            self.check()
        except BaseException as exc:
            result.update(error=str(exc),applied=False)
            try:restore_correctors(self.backend,g['actuators'],g['centers']);result['restored']=True
            except BaseException as restore_error:
                result.update(restoration_error=str(restore_error),restored=False)
                raise
            raise
        finally:
            previous=resume.get('orbit_measurements',[]) if resume else []
            result['orbit_measurements']=previous+copy.deepcopy(self.reader.orbit_measurements)
        return result

    def reset(self,result):
        g=result.get('pending_geometry') or result.get('calibration',result)
        restore_correctors(self.backend,g['actuators'],g['centers'])
        result['applied']=False;result['restored']=True
