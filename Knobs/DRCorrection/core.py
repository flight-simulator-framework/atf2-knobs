"""Measured response corrections, reversible acquisition and portable sessions.

The UI never invokes SAD shell scripts. CALC probes restore their settings;
SET is separate and refuses stale or foreign-backend proposals.
"""
from dataclasses import dataclass, asdict
import copy
import csv
import json
from pathlib import Path
import threading
import numpy as np
from scipy.optimize import lsq_linear
from .assessment import changed_indices,assess


class Cancelled(RuntimeError):
    pass


def serial(value):
    if isinstance(value,np.ndarray):return serial(value.tolist())
    if isinstance(value,np.generic):return serial(value.item())
    if isinstance(value,dict):return {k:serial(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [serial(v) for v in value]
    if isinstance(value,float) and not np.isfinite(value):return None
    return value


@dataclass
class Options:
    response_source: str = 'measured'
    model_match_tunes: bool = True
    model_regional_targets: bool = False
    model_fix_extraction_orbit: bool = False
    model_fix_extraction_dispersion: bool = False
    model_extraction_weight: float = 100.
    plane: str = 'y'
    count_h: int = 8
    count_v: int = 8
    energy_gev: float = 1.3
    tune_x: float = .205
    tune_y: float = .585
    tolerance_m: float = .0001
    frequency_change_khz: float = 1.
    momentum_compaction: float = .0021
    bpm_samples: int = 1
    bpm_interval_s: float = 0.
    sd: bool = True
    sf: bool = False
    sd_calibration: float = 0.
    sf_calibration: float = 0.
    probe1: str = ''
    probe2: str = ''
    change1_A: float = .2
    change2_A: float = .2
    corrector_probe_A: float = .05
    skew_probe: float = .0002
    step_A: float = .1
    skew_step: float = .0007
    svd_cut: float = .04
    dispersion_weight: float = .05
    orbit_weight: float = 1.
    excluded_bpms: tuple = ()
    fixed_actuators: tuple = ()


def checked_orbit(value,names):
    if value.get('position_unit')!='mm':raise ValueError('Orbit position_unit must be mm')
    if list(value['names'])!=list(names):raise ValueError('BPM order changed')
    x=np.asarray(value['x'],float).reshape(-1)
    y=np.asarray(value['y'],float).reshape(-1)
    q=np.asarray(value['tmit'],float).reshape(-1)
    valid=np.asarray(value.get('valid',np.ones(len(names),bool)),bool).reshape(-1)
    if any(v.shape!=(len(names),) for v in (x,y,q,valid)):
        raise ValueError('BPM array length mismatch')
    return x,y,valid & np.isfinite(x) & np.isfinite(y) & np.isfinite(q) & (q>0)


def validate_bpm_sampling(opt):
    if not np.isfinite(opt.bpm_samples) or int(opt.bpm_samples)!=opt.bpm_samples or not 1<=opt.bpm_samples<=1000:
        raise ValueError('BPM samples must be an integer in 1..1000')
    if not np.isfinite(opt.bpm_interval_s) or opt.bpm_interval_s<0:
        raise ValueError('BPM interval must be nonnegative')


class CorrectionService:
    def __init__(self,backend):
        self.backend=backend
        self.cancel=threading.Event()
        self.undo=[]
        self.orbit_measurements=[]
        self._model_manager=None

    @property
    def model_manager(self):
        if self._model_manager is None:
            from Knobs.DRTuning.model import ModelManager
            self._model_manager=ModelManager(self.backend)
        return self._model_manager

    def check_cancel(self):
        if self.cancel.is_set():raise Cancelled('Cancelled; temporary scan settings restored')

    def _orbit(self,opt):
        count=opt.bpm_samples;interval=opt.bpm_interval_s
        validate_bpm_sampling(opt)
        samples=[]
        for _ in range(int(count)):
            self.check_cancel()
            # Same sampling cadence as SysID, but wait BEFORE each sample
            # (including the first) instead of after it, to avoid an immediate
            # pre-change read. Total scheduled wait remains count * interval.
            if interval and self.cancel.wait(interval):self.check_cancel()
            result=self.backend.orbit()
            self.check_cancel()
            x,y,valid=checked_orbit(result,self.backend.bpm_names)
            samples.append(dict(x=x.copy(),y=y.copy(),tmit=np.asarray(result['tmit'],float).copy(),valid=valid.copy()))
        arrays={key:np.stack([s[key] for s in samples]) for key in ('x','y','tmit','valid')}
        means={key:np.mean(arrays[key],axis=0) for key in ('x','y','tmit')}
        valid=np.all(arrays['valid'],axis=0)
        self.orbit_measurements.append(dict(names=list(self.backend.bpm_names),**means,valid=valid,
            stdx=np.std(arrays['x'],axis=0),stdy=np.std(arrays['y'],axis=0),
            nshots=int(count),sample_interval_s=float(interval),samples=arrays,
            position_unit='mm',freshness_verified=False))
        return means['x'],means['y'],valid

    def _signal(self,mode,opt):
        n=len(self.backend.bpm_names)
        if mode=='orbit':
            x,y,mask=self._orbit(opt)
            self.full_signal=np.array([x,y])
            return np.array([x if opt.plane=='x' else y]),mask
        if mode=='dispersion':
            origin=self.backend.frequency()
            frequency_state=self.backend.frequency_state()
            span=opt.frequency_change_khz*1000
            if not np.isfinite(span) or span<=0 or not np.isfinite(opt.momentum_compaction) or opt.momentum_compaction<=0:
                raise ValueError('Positive frequency span and momentum compaction required')
            self.backend.validate_frequency_targets([origin-span/2,origin+span/2])
            baseline=self._orbit(opt)
            try:
                frames=[]
                for sign in (-1,1):
                    self.backend.set_frequency(origin+sign*span/2)
                    frames.append(self._orbit(opt))
            finally:self.backend.restore_frequency(frequency_state)
            index=0 if opt.plane=='x' else 1
            delta=-span/(self.backend.nominal_frequency_hz*opt.momentum_compaction)
            self.full_signal=np.array([(frames[1][0]-frames[0][0])/delta,(frames[1][1]-frames[0][1])/delta,baseline[0],baseline[1]])
            return np.array([(frames[1][index]-frames[0][index])/delta,baseline[index]]),baseline[2]&frames[0][2]&frames[1][2]
        if mode=='skew':
            signals=[];mask=np.ones(n,bool)
            for name,span in ((opt.probe1,opt.change1_A),(opt.probe2,opt.change2_A)):
                if name not in self.backend.hcorrectors or not np.isfinite(span) or span<=0:
                    raise ValueError('Choose two valid horizontal probes with positive full current spans')
                origin=self.backend.read('correctors',[name])[0]
                try:
                    frames=[]
                    for sign in (-1,1):
                        self.backend.write('correctors',[name],[origin+sign*span/2])
                        frames.append(self._orbit(opt))
                finally:self.backend.write('correctors',[name],[origin])
                # Native op-base plots show the vertical orbit difference for
                # each horizontal excitation, not a hidden coupling coefficient.
                signals.append(frames[1][1]-frames[0][1]);mask &= frames[0][2]&frames[1][2]
            self.full_signal=np.array(signals)
            return np.array(signals),mask
        raise ValueError('Unknown correction mode')

    def acquire(self,mode,opt,progress=lambda *_:None):
        validate_bpm_sampling(opt)
        if opt.response_source=='rftrack':
            try:return self.model_manager.acquire(self,mode,opt,progress)
            except Exception:
                self.model_manager.model=None;self.model_manager.optics=None
                raise
        if opt.response_source!='measured':raise ValueError('Unknown response source')
        self.orbit_measurements=[]
        self.backend.validate_mode(mode)
        if opt.plane not in ('x','y'):raise ValueError('Select x or y')
        # Validate the nominal target before moving any magnet or RF setting.
        nominal=self.backend.target_dispersion(opt.plane) if mode=='dispersion' else None
        if nominal is not None and (np.shape(nominal)!=(len(self.backend.bpm_names),) or not np.isfinite(nominal).all()):
            raise ValueError('Invalid nominal dispersion target')
        if mode=='skew':
            names=[n for n in self.backend.skews if (opt.sd and n.startswith('SD')) or (opt.sf and n.startswith('SF'))]
            kind='skews';probe=opt.skew_probe
        else:
            names=list(self.backend.hcorrectors if opt.plane=='x' else self.backend.vcorrectors)
            kind='correctors';probe=opt.corrector_probe_A
        names=[n for n in names if n not in opt.fixed_actuators]
        if not names:raise ValueError('No enabled correctors')
        if not np.isfinite(probe) or probe<=0:raise ValueError('Positive probe required')
        baseline=self.backend.snapshot()
        centers=np.asarray(self.backend.read(kind,names),float)
        low,high=self.backend.limits(kind,names)
        measured,valid=self._signal(mode,opt)
        full_measured=self.full_signal.copy();columns=[];full_columns=[]
        # Each probe owns its restoration, including failures/cancellation.
        # _signal likewise restores RF and skew-excitation correctors. Do not
        # replay the full snapshot: it includes magnets this scan never touched.
        for j,name in enumerate(names):
            self.check_cancel();values=[];full_values=[]
            lo=max(low[j],centers[j]-probe);hi=min(high[j],centers[j]+probe)
            if hi<=lo:raise ValueError('No available probe range')
            try:
                for setting in (lo,hi):
                    delay=float(getattr(self.backend,'profile',{}).get('magnet_settle_s',0.))
                    progress(int(100*j/len(names)),f'{name}: set {setting:.6g} {self.backend.unit(kind)}; current readback + {delay:g} s extra wait; BPM {opt.bpm_samples} samples, {opt.bpm_interval_s:g} s interval (first read delayed)')
                    self.backend.write(kind,[name],[setting])
                    value,mask=self._signal(mode,opt);values.append(value);full_values.append(self.full_signal.copy());valid &= mask
            finally:self.backend.write(kind,[name],[centers[j]])
            columns.append(((values[1]-values[0])/(hi-lo)).reshape(-1))
            full_columns.append(((full_values[1]-full_values[0])/(hi-lo)).reshape(-1))
            progress(int(100*(j+1)/len(names)),f'{name}: response {j+1}/{len(names)}')
        target=np.zeros_like(measured)
        if mode=='dispersion':target[0]=nominal
        return dict(schema='dr-correction-1',mode=mode,options=asdict(opt),
            backend_id=self.backend.identity,names=list(self.backend.bpm_names),
            positions_m=list(self.backend.positions_m),actuators=names,kind=kind,
            actuator_unit=self.backend.unit(kind),baseline=baseline,centers=centers,
            measured=measured,target=target,response=np.column_stack(columns),valid=valid,
            orbit_measurements=self.orbit_measurements,
            full_measured=full_measured,full_response=np.column_stack(full_columns),
            full_signal_labels=['COD X','COD Y'] if mode=='orbit' else (['Dispersion X','Dispersion Y','COD X','COD Y'] if mode=='dispersion' else [opt.probe1,opt.probe2]),
            limits=[low,high],source='measured symmetric response',
            signal_labels=['COD'] if mode=='orbit' else (['Dispersion','COD'] if mode=='dispersion' else [opt.probe1,opt.probe2]))

    def calculate(self,mode,opt,progress=lambda *_:None,dataset=None):
        self.cancel.clear()
        numeric=(opt.svd_cut,opt.step_A,opt.skew_step,opt.dispersion_weight,opt.orbit_weight,opt.tolerance_m)
        if not np.isfinite(numeric).all() or not 0<=opt.svd_cut<1 or min(opt.step_A,opt.skew_step,opt.tolerance_m)<=0:
            raise ValueError('Invalid correction bounds, tolerance or SVD cutoff')
        if min(opt.dispersion_weight,opt.orbit_weight)<0 or opt.dispersion_weight+opt.orbit_weight<=0:
            raise ValueError('Nonnegative correction weights, at least one positive, required')
        data=self.acquire(mode,opt,progress) if dataset is None else copy.deepcopy(dataset)
        if data.get('schema')!='dr-correction-1' or data['mode']!=mode:
            raise ValueError('File is not a matching correction dataset')
        if data['options']['plane']!=opt.plane and mode!='skew':
            raise ValueError('Select the same H/V plane as the file before CALC (FILE)')
        for key in ('after','full_after','after_valid','after_orbit_measurements','assessment','applied_actuators'):data.pop(key,None)
        names=data['names'];actuators=data['actuators']
        measured=np.asarray(data['measured'],float);target=np.asarray(data['target'],float)
        response=np.asarray(data['response'],float)
        if measured.shape!=target.shape or measured.ndim!=2 or measured.shape[1]!=len(names):
            raise ValueError('Signal shape mismatch')
        if response.shape!=(measured.size,len(actuators)) or not np.isfinite(response).all():
            raise ValueError('Invalid response matrix')
        valid=np.asarray(data['valid'],bool)&np.array([n not in opt.excluded_bpms for n in names])
        valid &= np.isfinite(measured).all(axis=0)&np.isfinite(target).all(axis=0)
        mask=np.tile(valid,measured.shape[0])
        available=np.array([n not in opt.fixed_actuators for n in actuators])
        if mode=='skew':available &= np.array([(opt.sd and n.startswith('SD')) or (opt.sf and n.startswith('SF')) for n in actuators])
        if not valid.any() or not available.any():raise ValueError('No valid enabled BPMs or actuators')
        weights=np.ones_like(measured)
        if mode=='dispersion':weights[0]*=opt.dispersion_weight;weights[1]*=opt.orbit_weight
        a=(response*weights.reshape(-1,1))[mask]
        residual=((measured-target)*weights).reshape(-1)[mask]
        constraints=data.get('constraints')
        if constraints is not None:
            cm=np.asarray(constraints['matrix'],float);cr=np.asarray(constraints['residual'],float);cw=float(constraints['weight'])
            if cm.shape!=(len(cr),len(actuators)) or not np.isfinite(cm).all() or not np.isfinite(cr).all() or not np.isfinite(cw) or cw<=0:
                raise ValueError('Invalid model extraction constraints')
            a=np.vstack((a,cw*cm));residual=np.concatenate((residual,cw*cr))
        maximum=(opt.count_h if opt.plane=='x' else opt.count_v) if mode!='skew' else len(actuators)
        maximum=min(maximum,int(available.sum()))
        if maximum<1:raise ValueError('At least one steering magnet required')
        centers=np.asarray(data['centers'],float)
        if centers.shape!=(len(actuators),) or not np.isfinite(centers).all():raise ValueError('Invalid center settings')
        lower_absolute=np.broadcast_to(np.asarray(data['limits'][0],float),centers.shape)
        upper_absolute=np.broadcast_to(np.asarray(data['limits'][1],float),centers.shape)
        if not np.isfinite(data['limits']).all() or np.any(lower_absolute>=upper_absolute) or np.any(centers<lower_absolute) or np.any(centers>upper_absolute):
            raise ValueError('Center settings outside recorded limits')
        step_limit=opt.skew_step if mode=='skew' else opt.step_A
        lower=np.maximum(lower_absolute-centers,-step_limit)
        upper=np.minimum(upper_absolute-centers,step_limit)
        if not 0<=opt.svd_cut<1 or step_limit<=0:raise ValueError('Invalid SVD cutoff or correction step limit')
        # SAD-style greedy corrector selection, followed by bounded least squares.
        selected=[];remaining=list(np.flatnonzero(available));best=None
        def solve(indices):
            matrix=a[:,indices];u,s,vt=np.linalg.svd(matrix,full_matrices=False)
            keep=s>max(s[0]*opt.svd_cut,1e-14) if len(s) else np.zeros(0,bool)
            if not keep.any():return np.zeros(len(indices)),s,0
            fit=lsq_linear(s[keep,None]*vt[keep],-(u[:,keep].T@residual),bounds=(lower[indices],upper[indices]))
            if not fit.success:raise RuntimeError('Bounded solve failed')
            return fit.x,s,int(keep.sum())
        symmetric=np.all(lower[available]==-step_limit) and np.all(upper[available]==step_limit)
        response_rms=np.sqrt(np.mean(a[:,available]**2,axis=0))
        diagnostic=', '.join(f'{name}: {value:.6g}' for name,value in zip(np.asarray(actuators)[available],response_rms))
        diagnostic=f'plane={opt.plane}; valid BPMs={int(valid.sum())}; weighted response RMS per {data["actuator_unit"]}: [{diagnostic}]'
        if np.linalg.norm(residual)>1e-14 and not np.any(a[:,available]):
            raise ValueError('No measured response: all enabled response columns are zero. '+diagnostic+
                '. Check magnet readbacks, BPM updates and settling; no correction was applied.')
        if symmetric and constraints is None and np.linalg.norm(residual)>1e-14:
            from .ported_solver import propose,ClosedOrbitError
            try:delta,selected_names,singular_values,rank=propose(data,opt,valid,available,maximum,step_limit)
            except ClosedOrbitError as exc:
                if str(exc)!='No useful corrector columns were selected':raise
                raise ValueError('Measured response could not reduce the selected COD/dispersion residual within the correction limits. '+diagnostic+
                    '. Check selected magnets/plane and BPM updates; no correction was applied.') from exc
            selected=[actuators.index(n) for n in selected_names]
            solver='ATFDRRingCorrection.propose_'+{'orbit':'orbit_correction','dispersion':'dispersion_correction','skew':'coupling_correction'}[mode]
        elif np.linalg.norm(residual)<=1e-14:
            delta=np.zeros(len(actuators));singular_values=[];rank=0;solver='Already at target'
        else:
            # The existing port accepts symmetric delta limits. Near an
            # absolute current limit, use an asymmetric bounded extension.
            for _ in range(maximum):
                candidates=[]
                for index in remaining:
                    self.check_cancel();selection=selected+[index];step,sv,rank=solve(selection)
                    candidates.append((np.linalg.norm(a[:,selection]@step+residual),index,step,sv,rank))
                best=min(candidates,key=lambda item:item[0]);selected.append(best[1]);remaining.remove(best[1])
                if mode=='orbit' and np.max(abs(a[:,selected]@best[2]+residual))<=opt.tolerance_m*1000:break
            delta=np.zeros(len(actuators));delta[selected]=best[2];singular_values=best[3];rank=best[4]
            solver='Bounded response fit with model extraction constraints' if constraints else 'Asymmetric bounded extension at absolute current limit'
        self.check_cancel()
        predicted=measured+(response@delta).reshape(measured.shape)
        data.update(options=asdict(opt),delta=delta,predicted=predicted,
            fitted=target+measured-predicted,valid=valid,selected=[actuators[i] for i in selected],
            singular_values=singular_values,rank=rank,solver=solver,applied=False)
        if constraints is not None:data['constraints']['predicted_change']=cm@delta
        if 'full_response' in data:
            full=np.asarray(data['full_measured'],float)
            data['full_predicted']=full+(np.asarray(data['full_response'])@delta).reshape(full.shape)
            data['full_fitted']=full-data['full_predicted']
        return data

    def apply(self,data):
        self.check_cancel()
        validate_bpm_sampling(Options(**data['options']))
        if data.get('backend_id')!=self.backend.identity:raise ValueError('Proposal belongs to another backend')
        if data.get('applied'):raise ValueError('Already applied; CALC again before another SET')
        changed=changed_indices(data)
        if not len(changed):raise ValueError('No magnet settings change; CALC already proposes zero correction')
        if not self.backend.same_settings(self.backend.snapshot(),data['baseline']):raise ValueError('Settings changed since CALC; recalculate')
        if data.get('model_settings'):
            from Knobs.DRTuning.model import verify_inputs
            verify_inputs(self.backend,data['model_settings'])
        self.backend.validate_mode(data['mode'])
        before=self.backend.snapshot()
        self.orbit_measurements=[]
        try:
            values=np.asarray(data['centers'])+np.asarray(data['delta'])
            self.backend.write(data['kind'],[data['actuators'][i] for i in changed],values[changed])
            measured,valid=self._signal(data['mode'],Options(**data['options']))
        except Exception:
            self.backend.restore(before);raise
        self.undo.append(before)
        indices=[self.backend.bpm_names.index(n) for n in data['names']]
        data['after']=measured[:,indices];data['after_valid']=valid[indices];data['applied']=True
        data['full_after']=self.full_signal[:,indices].copy()
        data['after_orbit_measurements']=self.orbit_measurements
        data['applied_actuators']=[data['actuators'][i] for i in changed]
        data['assessment']=assess(data)
        return data

    def reset(self):
        if not self.undo:raise ValueError('No SET operation to reset')
        self.backend.restore(self.undo[-1]);self.undo.pop()
        return self.backend.orbit()


def save_session(path,data):
    Path(path).write_text(json.dumps(serial(data),indent=2,allow_nan=False)+'\n')


def load_session(path):
    data=json.loads(Path(path).read_text())
    if data.get('schema') not in ('dr-correction-1','dr-tbt-1','dr-orbit-1','dr-sr-fit-1'):
        raise ValueError('Unsupported data schema')
    return data


def export_curves(path,data):
    with Path(path).open('w',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['signal','bpm','s_m','valid','measured','fitted','predicted','target','after'])
        for row,label in enumerate(data['signal_labels']):
            for i,name in enumerate(data['names']):
                writer.writerow([label,name,data['positions_m'][i],bool(data['valid'][i]),
                    *[data[k][row][i] if k in data else '' for k in ('measured','fitted','predicted','target','after')]])
        if 'full_measured' in data:
            for row,label in enumerate(data['full_signal_labels']):
                for i,name in enumerate(data['names']):
                    writer.writerow([label,name,data['positions_m'][i],bool(data['valid'][i]),
                        *[data['full_'+k][row][i] if 'full_'+k in data else '' for k in ('measured','fitted','predicted','target','after')]])
