"""Measured local bump closure and one-dimensional XSR size optimization."""
from dataclasses import dataclass,asdict
import copy
import time
import numpy as np
from Knobs.DRCorrection.core import CorrectionService,Options,Cancelled
from .bump_maps import constrained_step,select_geometry
from .model import name,verify_inputs


@dataclass
class BumpOptions:
    amplitude_mm: float = .4
    refine_mm: float = .1
    calibration_mm: float = .1
    probe_A: float = .05
    max_step_A: float = 4.
    outside_max_mm: float = .01
    outside_rms_mm: float = .005
    position_tolerance_mm: float = .01
    angle_tolerance_mrad: float = .01
    max_iterations: int = 4
    bpm_samples: int = 10
    xsr_samples: int = 10
    settle_s: float = 1.

    def validate(self):
        for key,value in asdict(self).items():
            if not np.isfinite(value) or value<0 or (key!='settle_s' and value==0):
                raise ValueError('Invalid bump option: '+key)
        if self.bpm_samples!=10 or self.xsr_samples!=10:raise ValueError('BPM and XSR acquisition require 10 updates')
        if self.calibration_mm>self.amplitude_mm or self.refine_mm>self.amplitude_mm:
            raise ValueError('Calibration / refinement exceeds scan range')
        if int(self.max_iterations)!=self.max_iterations:raise ValueError('Iteration count must be integer')


class BumpRangeUnavailable(ValueError):
    """Current bounds or measured closure prevent this displacement, not an IO fault."""


def fit_size(points,amplitude_mm,bounds_mm=None):
    """Fit sigma_y squared, then display sqrt(quadratic) in physical size units."""
    x=np.array([p['amplitude_mm'] for p in points]);y=np.array([p['xsr']['mean'] for p in points])
    err=np.array([p['xsr']['sem'] for p in points])
    if len(x)<5 or len(np.unique(x))<3 or not np.isfinite(np.r_[x,y,err]).all() or (y<=0).any():
        raise ValueError('Need at least five valid XSR measurements for a size-squared fit')
    uncertainty=np.maximum(2*y*err,max(float(np.median(y*y))*1.e-6,1.e-12))
    matrix=np.column_stack((x*x,x,np.ones(len(x))))/uncertainty[:,None]
    coefficients,_,rank,_=np.linalg.lstsq(matrix,y*y/uncertainty,rcond=1.e-10)
    if rank!=3:raise ValueError('Rank-deficient XSR fit')
    residual=(np.polyval(coefficients,x)-y*y)/uncertainty
    covariance=np.linalg.inv(matrix.T@matrix)*max(float(residual@residual)/(len(x)-3),1.)
    curvature=float(coefficients[0])
    if curvature<=0 or curvature<=2*np.sqrt(covariance[0,0]):
        raise ValueError('XSR fit has no resolved positive curvature; baseline restored')
    limits=(-amplitude_mm,amplitude_mm) if bounds_mm is None else bounds_mm
    best=float(-coefficients[1]/(2*curvature));low=max(limits[0],float(min(x)));high=min(limits[1],float(max(x)))
    if not low<best<high or np.polyval(coefficients,best)<=0:
        raise ValueError('XSR minimum is not bracketed inside the measured scan range; baseline restored')
    grid=np.linspace(low,high,201);size2=np.polyval(coefficients,grid)
    if np.any(size2<=0):raise ValueError('Unphysical fitted XSR size')
    return dict(coefficients_size_squared=coefficients,covariance=covariance,best_mm=best,
                predicted_size_um=float(np.sqrt(np.polyval(coefficients,best))),
                amplitude_mm=grid,size_um=np.sqrt(size2),weighted_residual=residual,
                method='weighted quadratic in sigma_y squared; positive curvature and bracket required')


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

    def check(self):self.reader.check_cancel()

    def ranked_targets(self):
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

    def orbit(self,opt):
        x,y,valid=self.reader._orbit(Options(bpm_samples=10))
        return np.r_[x,y],valid

    def prepare(self,target,opt,model_options,progress=lambda *_:None):
        opt.validate();self.backend.validate_mode('orbit');self.check()
        model=self.reader.model_manager.prepare(model_options,self.check,progress)
        _,valid=self.orbit(opt)
        indices=np.array([self.backend.bpm_names.index(n) for n in model.bpm_names],int)
        try:
            geometry=select_geometry(model,target,valid[indices],excluded=model_options.excluded_bpms,
                                     fixed=model_options.fixed_actuators,amplitude_mm=opt.amplitude_mm,max_step_A=opt.max_step_A)
            geometry['selection_half_range_mm']=opt.amplitude_mm
        except ValueError as exc:
            if not str(exc).startswith('No observable, bounded local bump geometry'):raise
            # Geometry/calibration need only a small bidirectional probe. The
            # measured scan-range search below verifies the larger displacements.
            geometry=select_geometry(model,target,valid[indices],excluded=model_options.excluded_bpms,
                                     fixed=model_options.fixed_actuators,amplitude_mm=opt.calibration_mm,max_step_A=opt.max_step_A)
            geometry['selection_half_range_mm']=opt.calibration_mm
        # The model may omit explicitly excluded / unmapped BPMs. Lift its
        # matrices to the acquisition order, keeping excluded columns zero.
        total=len(self.backend.bpm_names);mapping=np.r_[indices,indices+total]
        reconstruction=np.zeros((4,2*total));reconstruction[:,mapping]=geometry['reconstruction']
        response=np.zeros((2*total,len(geometry['actuators'])));response[mapping]=geometry['model_response']
        outside=indices[np.asarray(geometry['outside_indices'],int)]
        positions=np.asarray(self.backend.positions_m).copy();positions[indices]=geometry['positions_m']
        geometry.update(reconstruction=reconstruction,model_response=response,outside_indices=outside,
                        outside_rows=np.r_[outside,outside+total],
                        internal_indices=indices[np.asarray(geometry['internal_indices'],int)],
                        bpm_names=list(self.backend.bpm_names),positions_m=positions,
                        model_bpm_names=list(model.bpm_names))
        geometry['baseline']=self.backend.snapshot();geometry['options']=asdict(opt)
        geometry['model_match_tunes']=bool(model_options.model_match_tunes)
        progress(100,'BPMs and correctors selected; no magnet changed')
        return geometry

    def verify_geometry(self,geometry):
        if list(self.backend.bpm_names)!=list(geometry['bpm_names']):raise ValueError('BPM mapping changed')
        if not self.backend.same_settings(self.backend.snapshot(),geometry['baseline']):
            raise ValueError('Settings changed since selection / calibration; prepare again')
        if geometry.get('model_settings'):verify_inputs(self.backend,geometry['model_settings'])

    def move(self,names,values,opt):
        self.check();self.backend.write('correctors',names,values)
        actual=self.backend.read('correctors',names)
        tolerance=getattr(self.backend,'profile',{}).get('current_tolerance_A',1.e-5)
        if not np.isfinite(actual).all() or np.max(abs(actual-values))>tolerance:
            raise RuntimeError('Bump corrector readback mismatch')
        if self.cancel.wait(opt.settle_s):self.check()
        return actual

    def observe(self,g,baseline,opt):
        orbit,valid=self.orbit(opt)
        required=np.unique(np.r_[g['outside_indices'],g['internal_indices']])
        if not valid[required].all():raise ValueError('Required bump BPM lost beam / validity')
        delta=np.where(np.tile(valid,2),np.nan_to_num(orbit-baseline,nan=0.,posinf=0.,neginf=0.),0.)
        state=np.asarray(g['reconstruction'])@delta
        outside=delta[np.asarray(g['outside_rows'],int)]
        return dict(orbit_mm=orbit,valid=valid,state=state,outside_max_mm=float(np.max(abs(outside))),
                    outside_rms_mm=float(np.sqrt(np.mean(outside**2))),outside=outside)

    @staticmethod
    def closed(observation,amplitude,opt):
        state=observation['state'];desired=np.array([0,0,amplitude,0.])
        tolerance=np.array([opt.position_tolerance_mm,opt.angle_tolerance_mrad]*2)
        return (observation['outside_max_mm']<=opt.outside_max_mm and
                observation['outside_rms_mm']<=opt.outside_rms_mm and np.all(abs(state-desired)<=tolerance))

    def close_at(self,g,response,knob,baseline,amplitude,opt,event):
        names=g['actuators'];centers=np.asarray(g['centers']);lower,upper=np.asarray(g['limits'])
        lower=np.maximum(lower,centers-opt.max_step_A);upper=np.minimum(upper,centers+opt.max_step_A)
        command=centers+amplitude*knob
        if np.any(command<lower-1.e-9) or np.any(command>upper+1.e-9):raise BumpRangeUnavailable('Scaled bump exceeds current / step bounds')
        command=np.clip(command,lower,upper)  # Roundoff only, after the strict physical-bound check.
        constraint=np.asarray(g['reconstruction'])@response;desired=np.array([0.,0.,amplitude,0.])
        for iteration in range(opt.max_iterations+1):
            currents=self.move(names,command,opt)
            observed=self.observe(g,baseline,opt)
            observed.update(amplitude_mm=float(amplitude),iteration=iteration,currents_A=currents.copy())
            event(dict(kind='orbit',**observed))
            if self.closed(observed,amplitude,opt):return observed
            if iteration==opt.max_iterations:break
            try:
                step=constrained_step(response[np.asarray(g['outside_rows'],int)],-observed['outside'],constraint,
                                      desired-observed['state'],lower-command,upper-command)
            except ValueError as exc:
                raise BumpRangeUnavailable('Bump closure cannot reach this displacement: '+str(exc)) from exc
            command=command+step
        raise BumpRangeUnavailable('Beam-based bump did not close within COD / target tolerances')

    def calibrate(self,geometry,opt,progress=lambda *_:None,event=lambda *_:None):
        opt.validate();self.backend.validate_mode('orbit');self.verify_geometry(geometry);g=copy.deepcopy(geometry);names=g['actuators']
        self.last_result=g
        centers=np.asarray(g['centers']);lower,upper=np.asarray(g['limits'])
        baseline,valid=self.orbit(opt);columns=[];self.reader.orbit_measurements=[]
        required=np.unique(np.r_[g['outside_indices'],g['internal_indices']])
        if not valid[required].all():raise ValueError('Required BPM invalid before calibration')
        spans=np.minimum(opt.probe_A,np.minimum(centers-lower,upper-centers))
        if (spans<=1.e-6).any():raise ValueError('No symmetric probe margin for selected correctors')
        try:
            for j,(n,center,span) in enumerate(zip(names,centers,spans)):
                values=[];readbacks=[]
                try:
                    for sign in (-1,1):
                        readbacks.append(float(self.move([n],np.array([center+sign*span]),opt)[0]))
                        orbit,mask=self.orbit(opt)
                        if not mask[required].all():raise ValueError('Required BPM invalid during corrector probe')
                        values.append(np.where(np.tile(mask,2),orbit,0.))
                finally:restore_correctors(self.backend,[n],[center])
                measured_span=readbacks[1]-readbacks[0]
                if measured_span<=max(1.e-6,span*.2):raise ValueError('No resolved corrector current probe: '+n)
                columns.append((values[1]-values[0])/measured_span)
                g.setdefault('probe_readbacks',[]).append(dict(magnet=n,requested=[center-span,center+span],actual=readbacks))
                progress(round(70*(j+1)/len(names)),f'Measured bump response {n} ({j+1}/{len(names)})')
            response=np.column_stack(columns)
            margin=np.minimum(opt.max_step_A,np.minimum(centers-lower,upper-centers))/opt.calibration_mm
            knob=constrained_step(response[np.asarray(g['outside_rows'],int)],np.zeros(len(g['outside_rows'])),
                                  np.asarray(g['reconstruction'])@response,[0,0,1,0],-margin,margin)
            checks=[]
            for amplitude in (opt.calibration_mm,-opt.calibration_mm):
                observed=self.close_at(g,response,knob,baseline,amplitude,opt,event);checks.append(observed)
                # Keep a common linear knob. Scan points still verify actual closure.
                if amplitude>0:knob=(observed['currents_A']-centers)/amplitude
            knob=(checks[0]['currents_A']-checks[1]['currents_A'])/(2*opt.calibration_mm)
            if np.any(abs(knob)>margin+1.e-10):raise ValueError('Calibrated bump exceeds the calibration current margin')
            g.update(response=response,knob_A_per_mm=knob,reference_orbit_mm=baseline,calibration_checks=checks,
                     calibrated=False,options=asdict(opt),orbit_measurements=copy.deepcopy(self.reader.orbit_measurements))
        except BaseException as exc:
            g.update(error=str(exc),calibrated=False)
            raise
        finally:
            g['orbit_measurements']=copy.deepcopy(self.reader.orbit_measurements)
            try:restore_correctors(self.backend,names,centers);g['restored']=True
            except BaseException as exc:
                g.update(restoration_error=str(exc),restored=False,calibrated=False)
                raise
        self.check();self.verify_geometry(geometry);g['calibrated']=True;self.last_result=g
        progress(100,'Measured bump calibrated in both signs; baseline currents restored')
        return g

    def select_scan_range(self,g,opt,result,progress=lambda *_:None,event=lambda *_:None):
        """Test contiguous windows around zero, prioritizing width over centering.

        The default requested width is 0.8 mm, with a 0.4 mm fallback. Window
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
        for width in (2*opt.amplitude_mm,opt.amplitude_mm):
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
                # always move and recheck COD before and after their XSR reads.
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
                            restore_correctors(self.backend,g['actuators'],g['centers'])
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
        raise BumpRangeUnavailable('No closed scan window at the requested width or half-width; inspect range checks')

    def scan(self,calibration,opt,progress=lambda *_:None,event=lambda *_:None):
        opt.validate()
        if not hasattr(self.backend,'sample_xsr'):raise ValueError('Backend has no measured XSR size acquisition')
        if not calibration.get('calibrated'):raise ValueError('Calibrate the local bump first')
        if calibration['options']!=asdict(opt):raise ValueError('Bump options changed; calibrate again')
        self.backend.validate_mode('orbit');self.verify_geometry(calibration);self.check()
        g=calibration;baseline=np.asarray(g['reference_orbit_mm']);knob=np.asarray(g['knob_A_per_mm'])
        response=np.asarray(g['response']);self.reader.orbit_measurements=[]
        result=dict(schema='dr-coupling-bump-1',calibration=copy.deepcopy(g),options=asdict(opt),
                    points=[],fits=[],started_unix_s=time.time(),applied=False)
        self.last_result=result
        def measure(amplitude,phase):
            observed=self.close_at(g,response,knob,baseline,float(amplitude),opt,event)
            actual_amplitude=float(observed['state'][2])
            def sample(value):event(dict(kind='sample',amplitude_mm=actual_amplitude,requested_amplitude_mm=float(amplitude),phase=phase,xsr=value))
            xsr=self.backend.sample_xsr(self.cancel,count=10,callback=sample)
            self.check()
            if xsr['count']!=10 or not xsr.get('update_verified'):raise ValueError('Ten fresh XSR updates required')
            # Verify closure after the XSR integration as well as before it.
            after=self.observe(g,baseline,opt)
            if not self.closed(after,amplitude,opt):raise ValueError('COD drifted during XSR acquisition; baseline restored')
            point=dict(amplitude_mm=actual_amplitude,requested_amplitude_mm=float(amplitude),phase=phase,xsr=xsr,orbit=observed,orbit_after_xsr=after)
            result['points'].append(point);event(dict(kind='point',point=point))
            return point
        try:
            selected=self.select_scan_range(g,opt,result,progress,event)
            low,high=selected['bounds_mm']
            for i,a in enumerate(np.linspace(low,high,5)):
                measure(a,'coarse');progress(10+10*i,f'XSR scan {a:+.3f} mm: 10 updates')
            result['observed_coarse_range_mm']=[min(p['amplitude_mm'] for p in result['points']),max(p['amplitude_mm'] for p in result['points'])]
            fit=fit_size(result['points'],opt.amplitude_mm,selected['bounds_mm']);result['fits'].append(fit);event(dict(kind='fit',fit=fit))
            # Near a boundary the additional points stay within the authorized range.
            refinement=np.clip([fit['best_mm']-opt.refine_mm,fit['best_mm']+opt.refine_mm],low,high)
            result['refinement_mm']=refinement
            for a in refinement:measure(a,'refinement')
            fit=fit_size(result['points'],opt.amplitude_mm,selected['bounds_mm']);result['fits'].append(fit);event(dict(kind='fit',fit=fit))
            final=measure(fit['best_mm'],'final verification')
            best_measured=min(result['points'][:-1],key=lambda p:p['xsr']['mean'])
            tolerance=3*np.hypot(final['xsr']['sem'],best_measured['xsr']['sem'])+1.e-3
            if final['xsr']['mean']>best_measured['xsr']['mean']+tolerance:
                raise ValueError('Verified fitted optimum is worse than the best measured size; baseline restored')
            result.update(applied=True,final_amplitude_mm=fit['best_mm'],final=final,
                          final_observed_amplitude_mm=final['amplitude_mm'],
                          final_currents_A=self.backend.read('correctors',g['actuators']))
            self.check();progress(100,f"Set fitted optimum {fit['best_mm']:+.4f} mm; XSR and COD verified")
        except BaseException as exc:
            result.update(error=str(exc),applied=False)
            try:restore_correctors(self.backend,g['actuators'],g['centers']);result['restored']=True
            except BaseException as restore_error:
                result.update(restoration_error=str(restore_error),restored=False)
                raise
            raise
        finally:result['orbit_measurements']=copy.deepcopy(self.reader.orbit_measurements)
        return result

    def reset(self,result):
        g=result.get('calibration',result);restore_correctors(self.backend,g['actuators'],g['centers'])
        result['applied']=False;result['restored']=True
