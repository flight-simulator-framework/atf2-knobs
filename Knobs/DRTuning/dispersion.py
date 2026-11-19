"""Measured straight-section horizontal dispersion correction with QM trims."""
from dataclasses import dataclass,asdict
import copy
import re
import numpy as np
from scipy.optimize import minimize,LinearConstraint
from Knobs.DRCorrection.core import CorrectionService,Options
from .model import name

QM_CANDIDATES=tuple(f'QM{i}R.{j}' for j in (1,2) for i in (1,2,3,4,20,21,22,23))


@dataclass
class DispersionOptions:
    probe_A: float = .1
    step_A: float = 2.
    frequency_change_khz: float = 4.
    momentum_compaction: float = .0021
    measure_tunes: bool = False
    tune_tolerance: float = .002
    orbit_tolerance_mm: float = .05
    dispersion_weight: float = .05
    regularization: float = .01
    svd_cut: float = .04
    selection: str = 'all'
    maximum: int = 8
    excluded_bpms: tuple = ()
    fixed_actuators: tuple = ('QM2R.1','QM2R.2')

    def validate(self):
        for k in ('probe_A','step_A','frequency_change_khz','momentum_compaction','orbit_tolerance_mm','dispersion_weight'):
            if not np.isfinite(getattr(self,k)) or getattr(self,k)<=0:raise ValueError('Invalid '+k)
        if not isinstance(self.measure_tunes,bool):raise ValueError('Invalid measure_tunes option')
        if self.measure_tunes and (not np.isfinite(self.tune_tolerance) or self.tune_tolerance<=0):raise ValueError('Invalid tune_tolerance')
        if (not np.isfinite([self.regularization,self.svd_cut]).all() or self.regularization<0 or
                not 0<=self.svd_cut<1 or self.selection not in ('all','greedy') or
                int(self.maximum)!=self.maximum or not 1<=self.maximum<=16):raise ValueError('Invalid dispersion solver options')


def straight_bpms(names):
    result=[]
    for n in names:
        match=re.fullmatch(r'MB(\d+)R',name(n));i=int(match[1]) if match else -1
        result.append(19<=i<=31 or 66<=i<=80)
    return np.asarray(result,bool)


def quad_limits(backend,n):
    p=backend.quadrupoles[n]
    if 'limits_A' in p:return tuple(map(float,p['limits_A']))
    return float(backend.get(p['lower_limit_pv'])),float(backend.get(p['upper_limit_pv']))


def restore_quads(backend,names,values):
    errors=[]
    for n,value in zip(names,values):
        try:
            if hasattr(backend,'restore_quadrupole_current'):backend.restore_quadrupole_current(n,float(value))
            else:backend.set_quadrupole_current(n,float(value))
        except Exception as exc:errors.append(n+': '+str(exc))
    if errors:raise RuntimeError('QM restoration incomplete: '+'; '.join(errors))


class DispersionService:
    def __init__(self,backend,cancel):
        self.backend=backend;self.reader=CorrectionService(backend);self.reader.cancel=cancel;self.last_result=None

    def check(self):self.reader.check_cancel()

    def signal(self,opt):
        self.check()
        o=Options(plane='x',bpm_samples=10,frequency_change_khz=opt.frequency_change_khz,
                  momentum_compaction=opt.momentum_compaction)
        _,valid=self.reader._signal('dispersion',o)
        if not opt.measure_tunes:
            return self.reader.full_signal.copy(),valid,None,dict(source='skipped',readback_update_verified=False,
                acquisition_quality='Tune acquisition and tune constraints disabled by option')
        tunes=self.backend.measure_tunes(cancel=self.reader.cancel)
        q=np.asarray(tunes['tunes'],float)
        if q.shape!=(2,) or not np.isfinite(q).all():raise ValueError('Invalid tune measurement')
        return self.reader.full_signal.copy(),valid,q,tunes

    def calculate(self,opt,progress=lambda *_:None,event=lambda *_:None):
        opt.validate();self.check();b=self.backend;b.validate_mode('dispersion')
        if opt.measure_tunes and hasattr(b,'validate_tune_measurement'):b.validate_tune_measurement()
        if not opt.measure_tunes:progress(0,'Tune acquisition / tune constraints skipped; measuring dispersion and COD')
        names=[n for n in QM_CANDIDATES if n not in opt.fixed_actuators]
        missing=set(names)-set(getattr(b,'quadrupoles',{}))
        if missing:raise ValueError('Confirmed QM trim mapping / calibration missing: '+', '.join(sorted(missing)))
        if not names:raise ValueError('No enabled QM trims')
        enabled=np.array([n not in opt.excluded_bpms for n in b.bpm_names]);straight=straight_bpms(b.bpm_names)
        if not np.any(straight&enabled):raise ValueError('No enabled BPM in MB19R–MB31R / MB66R–MB80R')
        centers=np.array([b.quadrupole_current(n) for n in names]);limits=np.array([quad_limits(b,n) for n in names]).T
        if not np.isfinite(np.r_[centers,limits.ravel()]).all() or np.any(limits[0]>=limits[1]):raise ValueError('Invalid QM limits / current')
        spans=np.minimum(opt.probe_A,np.minimum(centers-limits[0],limits[1]-centers))
        if np.any(spans<=1.e-6):raise ValueError('No symmetric QM probe margin; exclude the limited trim')
        for n,c,h in zip(names,centers,spans):b.validate_quadrupole_targets(n,[c-h,c,c+h])
        baseline=b.snapshot();self.reader.orbit_measurements=[]
        measured,valid,tunes,tune_evidence=self.signal(opt);valid &= enabled
        if not np.any(valid&straight):raise ValueError('No valid straight-section BPM before QM probes')
        data=dict(schema='dr-qm-dispersion-1',mode='dispersion',backend_id=b.identity,
                  names=list(b.bpm_names),positions_m=np.asarray(b.positions_m),actuators=names,
                  centers=centers,limits=limits,baseline=baseline,full_measured=measured,valid=valid,
                  straight=straight,initial_tunes=tunes,initial_tune_evidence=tune_evidence,options=asdict(opt),applied=False,
                  full_signal_labels=['Dispersion X','Dispersion Y','COD X','COD Y'],scan_tunes=[],probe_readbacks=[],
                  tune_measurement_enabled=opt.measure_tunes)
        self.last_result=data;event(dict(kind='dispersion',data=copy.deepcopy(data)))
        columns=[];qcolumns=[]
        try:
            for j,(n,c,h) in enumerate(zip(names,centers,spans)):
                frames=[];qs=[];readbacks=[]
                try:
                    for sign in (-1,1):
                        self.check();b.set_quadrupole_current(n,c+sign*h)
                        reading=float(b.quadrupole_current(n))
                        if not np.isfinite(reading):raise ValueError('Invalid QM current readback: '+n)
                        readbacks.append(reading)
                        frame,mask,q,evidence=self.signal(opt);valid &= mask
                        if not np.any(valid&straight):raise ValueError('Lost all common valid straight-section BPMs')
                        frames.append(frame)
                        if opt.measure_tunes:
                            qs.append(q);data['scan_tunes'].append(dict(magnet=n,current_A=c+sign*h,measurement=evidence))
                finally:restore_quads(b,[n],[c])
                measured_span=readbacks[1]-readbacks[0]
                if measured_span<=max(1.e-6,h*.2):raise ValueError('No resolved QM current probe: '+n)
                columns.append(((frames[1]-frames[0])/measured_span).ravel())
                if opt.measure_tunes:qcolumns.append(((qs[1]-qs[0]+.5)%1-.5)/measured_span)
                data['probe_readbacks'].append(dict(magnet=n,requested=[c-h,c+h],actual=readbacks))
                progress(round(90*(j+1)/len(names)),f'QM dispersion response {n} ({j+1}/{len(names)})')
            self.check()
            if not b.same_settings(b.snapshot(),baseline):raise ValueError('Settings changed during QM response scan; calculate again')
            data.update(response=np.column_stack(columns),tune_response=np.column_stack(qcolumns) if opt.measure_tunes else None,valid=valid,
                        orbit_measurements=copy.deepcopy(self.reader.orbit_measurements))
            self.solve(data,opt);event(dict(kind='dispersion',data=copy.deepcopy(data)))
            progress(100,'QM proposal ready; all diagnostic currents and RF restored')
            return data
        except BaseException as exc:
            data['error']=str(exc);data['orbit_measurements']=copy.deepcopy(self.reader.orbit_measurements)
            raise

    def solve(self,data,opt):
        opt.validate();valid=np.asarray(data['valid'],bool);straight=np.asarray(data['straight'],bool)
        if not np.any(valid&straight):raise ValueError('No common valid straight-section BPM across scan')
        measured=np.asarray(data['full_measured']);target=measured.copy();target[0,straight]=0.
        response=np.asarray(data['response']);mask=np.tile(valid,4)
        weights=np.repeat([opt.dispersion_weight,opt.dispersion_weight,1.,1.],len(valid))
        a=(response*weights[:,None])[mask];residual=((measured-target).ravel()*weights)[mask]
        c=np.asarray(data['centers']);lo,hi=np.asarray(data['limits'])
        lower=np.maximum(lo-c,-opt.step_A);upper=np.minimum(hi-c,opt.step_A)
        constraints=response[2*len(valid):][np.tile(valid,2)]
        tolerance=np.full(2*valid.sum(),opt.orbit_tolerance_mm)
        if opt.measure_tunes:
            tune_response=np.asarray(data.get('tune_response'),float)
            if tune_response.shape!=(2,len(c)):raise ValueError('No measured tune response; CALC with tune acquisition enabled first')
            constraints=np.vstack((tune_response,constraints))
            tolerance=np.r_[np.full(2,opt.tune_tolerance),tolerance]
        if not np.isfinite(np.r_[a.ravel(),residual,constraints.ravel()]).all():raise ValueError('Invalid measured QM response')
        def fit(indices):
            self.check();matrix=a[:,indices];u,s,vt=np.linalg.svd(matrix,full_matrices=False)
            keep=s>max(s[0]*opt.svd_cut,1.e-12)
            if not keep.any():raise ValueError('No usable QM response')
            reduced=s[keep,None]*vt[keep];rhs=u[:,keep].T@residual
            h=(reduced.T@reduced+(opt.regularization*s[0])**2*np.eye(len(indices)))/s[0]**2
            g=reduced.T@rhs/s[0]**2;cm=constraints[:,indices]/tolerance[:,None]
            result=minimize(lambda x:.5*x@h@x+g@x,np.zeros(len(indices)),jac=lambda x:h@x+g,
                bounds=list(zip(lower[indices],upper[indices])),constraints=LinearConstraint(cm,-1.,1.),
                method='SLSQP',options=dict(ftol=1.e-12,maxiter=500))
            if not result.success or np.max(abs(cm@result.x))>1+1.e-6:raise ValueError('Bounded QM solve failed: '+result.message)
            return result.x,float(np.linalg.norm(matrix@result.x+residual)),s
        available=list(range(len(c)));selected=[]
        if opt.selection=='all':selected=available;step,_,singular=fit(selected)
        else:
            score=float(np.linalg.norm(residual));step=np.zeros(0);singular=np.zeros(0)
            for _ in range(min(opt.maximum,len(c))):
                choices=[]
                for j in available:
                    x,norm,s=fit(selected+[j]);choices.append((norm,j,x,s))
                best=min(choices,key=lambda row:row[0])
                if best[0]>=score-1.e-12:break
                score,j,step,singular=best;selected.append(j);available.remove(j)
        delta=np.zeros(len(c));delta[selected]=step
        data.update(options=asdict(opt),delta=delta,full_target=target,full_predicted=measured+(response@delta).reshape(measured.shape),
                    predicted_tune_change=tune_response@delta if opt.measure_tunes else None,selected=[data['actuators'][i] for i in selected],
                    singular_values=singular,solver='bounded regularized SVD + '+
                    ('tune/COD constraints; ' if opt.measure_tunes else 'COD constraints; tune acquisition / constraints skipped; ')+opt.selection)
        return data

    def apply(self,data,progress=lambda *_:None,event=lambda *_:None):
        self.check();b=self.backend;saved_options=dict(data['options']);saved_options.setdefault('measure_tunes',True)
        opt=DispersionOptions(**saved_options);b.validate_mode('dispersion')
        if data.get('applied') or data['backend_id']!=b.identity:raise ValueError('Stale or already applied QM proposal')
        if not b.same_settings(b.snapshot(),data['baseline']):raise ValueError('Settings changed since QM CALC')
        names=data['actuators'];centers=np.asarray(data['centers']);delta=np.asarray(data['delta']);changed=np.flatnonzero(abs(delta)>1.e-12)
        if not len(changed):raise ValueError('No QM correction proposed')
        for j in changed:b.validate_quadrupole_targets(names[j],[centers[j]+delta[j]])
        self.reader.orbit_measurements=[]
        try:
            for j in changed:
                self.check();b.set_quadrupole_current(names[j],float(centers[j]+delta[j]))
            after,valid,tunes,evidence=self.signal(opt)
            mask=np.asarray(data['valid'],bool)
            if not valid[mask].all():raise ValueError('A fitted BPM became invalid after SET')
            dt=(tunes-np.asarray(data['initial_tunes'])+.5)%1-.5 if opt.measure_tunes else None
            orbit_change=after[2:,mask]-np.asarray(data['full_measured'])[2:,mask]
            data.update(full_after=after,after_valid=valid,after_tunes=tunes,after_tune_evidence=evidence,
                        tune_check_performed=opt.measure_tunes,
                        after_orbit_measurements=copy.deepcopy(self.reader.orbit_measurements))
            if opt.measure_tunes and np.max(abs(dt))>opt.tune_tolerance:
                raise ValueError('Measured tune change exceeds QM correction tolerance; restored')
            if np.max(abs(orbit_change))>opt.orbit_tolerance_mm:
                raise ValueError('Measured COD change exceeds QM correction tolerance; restored')
            straight=mask&np.asarray(data['straight'],bool)
            if np.linalg.norm(after[0,straight])>np.linalg.norm(np.asarray(data['full_measured'])[0,straight]):
                raise ValueError('Measured horizontal straight dispersion did not improve; restored')
            self.check();data['applied_currents_A']=[b.quadrupole_current(n) for n in names]
            data['applied']=True;event(dict(kind='dispersion',data=copy.deepcopy(data)))
            progress(100,'QM correction set; actual dispersion and COD remeasured; '+
                     ('tunes remeasured' if opt.measure_tunes else 'tune acquisition / constraints skipped'))
        except BaseException as exc:
            data.update(error=str(exc),applied=False)
            try:restore_quads(b,[names[j] for j in changed],centers[changed]);data['restored']=True
            except BaseException as restore_error:
                data.update(restoration_error=str(restore_error),restored=False)
                raise
            raise
        return data

    def reset(self,data):
        restore_quads(self.backend,data['actuators'],data['centers']);data['applied']=False;data['restored']=True
