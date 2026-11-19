"""Measured straight-section horizontal dispersion correction with QM trims."""
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
import copy
import hashlib
import json
import re
import numpy as np
from scipy.optimize import minimize,LinearConstraint
from Knobs.DRCorrection.core import CorrectionService,Options,serial
from .model import name
from .qm_checkpoint import prepare_destination,save_checkpoint

QM_CANDIDATES=tuple(f'QM{i}R.{j}' for j in (1,2) for i in (1,2,3,4,20,21,22,23))
PROBE_INCREMENT_A=.01


@dataclass
class DispersionOptions:
    probe_A: float = 3.
    adaptive_probe: bool = True
    probe_current_limit_A: float = 3.
    target_snr: float = 5.
    maximum_probe_pairs: int = 12
    probe_max_growth: float = 3.
    reuse_probe_widths: bool = True
    characterize_first_scan: bool = True
    step_A: float = 3.  # Legacy saved-file field; the shared absolute current limit controls the solver.
    frequency_change_khz: float = 4.
    momentum_compaction: float = .0021
    measure_tunes: bool = False
    tune_tolerance: float = .002
    constrain_cod: bool = False
    orbit_tolerance_mm: float = .05
    dispersion_weight: float = .05
    regularization: float = .01
    svd_cut: float = .04
    selection: str = 'all'
    maximum: int = 8
    excluded_bpms: tuple = ()
    fixed_actuators: tuple = ('QM2R.1','QM2R.2')

    def validate(self):
        for k in ('probe_A','probe_current_limit_A','target_snr','step_A','frequency_change_khz','momentum_compaction','orbit_tolerance_mm','dispersion_weight'):
            if not np.isfinite(getattr(self,k)) or getattr(self,k)<=0:raise ValueError('Invalid '+k)
        if not isinstance(self.measure_tunes,bool):raise ValueError('Invalid measure_tunes option')
        if not isinstance(self.constrain_cod,bool):raise ValueError('Invalid constrain_cod option')
        if not isinstance(self.adaptive_probe,bool) or int(self.maximum_probe_pairs)!=self.maximum_probe_pairs or not 1<=self.maximum_probe_pairs<=20:
            raise ValueError('Invalid adaptive QM probe options')
        if self.probe_A<PROBE_INCREMENT_A or not np.isclose(self.probe_A/PROBE_INCREMENT_A,round(self.probe_A/PROBE_INCREMENT_A),atol=1.e-9,rtol=0):
            raise ValueError('Initial QM probe width must be a multiple of 0.01 A, minimum 0.01 A')
        if not np.isfinite(self.probe_max_growth) or self.probe_max_growth<=1 or not isinstance(self.reuse_probe_widths,bool):
            raise ValueError('Invalid QM growth / width reuse options')
        if not isinstance(self.characterize_first_scan,bool):raise ValueError('Invalid first-pass option')
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
    if p.get('lower_limit_pv') and p.get('upper_limit_pv'):
        return float(backend.get(p['lower_limit_pv'])),float(backend.get(p['upper_limit_pv']))
    if 'limits_A' in p:return tuple(map(float,p['limits_A']))
    raise ValueError('No QM supply limits configured: '+n)


class ProbeRangeUnavailable(ValueError):
    """A QM cannot be probed within its limits and restored to its baseline."""


class ProbeSignalRejected(ValueError):
    """A restored probe may be retried at a smaller width; hardware errors are not caught here."""


class InsufficientProbeQuality(ValueError):
    pass


def probe_floor(value):return round(np.floor(value/PROBE_INCREMENT_A+1.e-10)*PROBE_INCREMENT_A,12)
def probe_ceil(value):return round(np.ceil(value/PROBE_INCREMENT_A-1.e-10)*PROBE_INCREMENT_A,12)


def probe_bounds(limits,opt):
    lower=max(float(limits[0]),-opt.probe_current_limit_A)
    upper=min(float(limits[1]),opt.probe_current_limit_A)
    lo=probe_ceil(lower);hi=probe_floor(upper)
    if lo<lower:lo=round(lo+PROBE_INCREMENT_A,12)
    if hi>upper:hi=round(hi-PROBE_INCREMENT_A,12)
    return lo,hi


def probe_pair(magnet,center,limits,opt,width):
    """Move the probe reference inside the allowed range, then clip the endpoints."""
    if not np.isfinite(limits).all() or limits[0]>=limits[1]:
        raise ValueError(f'Invalid QM supply limits for {magnet}: {limits}')
    lower,upper=probe_bounds(limits,opt)
    detail=(f'{magnet}: current={center:+.6g} A; supply=[{limits[0]:+.6g}, {limits[1]:+.6g}] A; '
            f'probe absolute limit=+/-{opt.probe_current_limit_A:g} A; allowed=[{lower:+.6g}, {upper:+.6g}] A')
    if not limits[0]<=center<=limits[1]:
        raise ProbeRangeUnavailable('Original current is outside supply limits; cannot guarantee restoration. '+detail)
    if upper-lower<PROBE_INCREMENT_A-1.e-9:raise ProbeRangeUnavailable('No 0.01 A QM probe span within limits. '+detail)
    reference=float(np.clip(center,lower,upper))
    targets=np.array([max(lower,probe_floor(reference-width)),min(upper,probe_ceil(reference+width))])
    if targets[1]-targets[0]<PROBE_INCREMENT_A-1.e-9:raise ProbeRangeUnavailable('No 0.01 A QM probe span within limits. '+detail)
    return targets,max(reference-lower,upper-reference)


def estimated_probe_width(center,limits,measured_span,quality,opt):
    """Infer a target width from measured span and S/N, including clipped pairs."""
    lo,hi=probe_bounds(limits,opt);reference=float(np.clip(center,lo,hi))
    near=min(reference-lo,hi-reference);far=max(reference-lo,hi-reference)
    snr=quality['snr']
    if snr is None or snr<=0:return None
    # A small headroom avoids repeated measurements just below the target.
    wanted_span=measured_span*1.1*opt.target_snr/snr
    estimate=wanted_span/2 if wanted_span<=2*near else wanted_span-near
    return min(probe_ceil(far),max(PROBE_INCREMENT_A,probe_ceil(estimate)))


def next_probe_width(width,center,limits,measured_span,quality,opt,failed_width=None):
    """Use S/N feedback with a growth cap and a remembered failed-width ceiling."""
    estimate=estimated_probe_width(center,limits,measured_span,quality,opt)
    _,far=probe_pair('',center,limits,opt,width)
    if estimate is None:estimate=width*opt.probe_max_growth
    ceiling=probe_floor(width*opt.probe_max_growth)
    ceiling=min(ceiling,probe_ceil(far))
    if failed_width is not None:ceiling=min(ceiling,round(failed_width-PROBE_INCREMENT_A,12))
    result=min(ceiling,max(round(width+PROBE_INCREMENT_A,12),probe_ceil(estimate)))
    return result if result>width+1.e-9 else None


def restore_quads(backend,names,values):
    errors=[]
    for n,value in zip(names,values):
        try:
            if hasattr(backend,'restore_quadrupole_current'):backend.restore_quadrupole_current(n,float(value))
            else:backend.set_quadrupole_current(n,float(value))
        except Exception as exc:errors.append(n+': '+str(exc))
    if errors:raise RuntimeError('QM restoration incomplete: '+'; '.join(errors))


class QMOrbitReader(CorrectionService):
    """Keep isolated good samples; reject a BPM after five consecutive bad updates."""
    invalid_streak_limit=5

    def reset_sampling(self):
        self.orbit_measurements=[]
        self.invalid_streak=np.zeros(len(self.backend.bpm_names),dtype=int)

    def summarize_orbit(self,arrays):
        good=arrays['valid'];count=good.sum(axis=0)
        streak=getattr(self,'invalid_streak',np.zeros(good.shape[1],dtype=int)).copy()
        longest=np.zeros_like(streak)
        for row in good:
            streak=np.where(row,0,streak+1);longest=np.maximum(longest,streak)
        self.invalid_streak=streak
        means={key:np.divide(np.where(good,arrays[key],0.).sum(axis=0),count,
                            out=np.full(count.shape,np.nan),where=count>0) for key in ('x','y','tmit')}
        std={}
        for key in ('x','y'):
            residual=np.where(good,arrays[key],0.)-np.where(count>0,means[key],0.)
            variance=np.divide(np.where(good,residual**2,0.).sum(axis=0),count,
                               out=np.full(count.shape,np.nan),where=count>0)
            std['std'+key]=np.sqrt(variance)
        return dict(**means,**std,valid=(count>0)&(longest<self.invalid_streak_limit),
                    valid_sample_count=count,max_invalid_streak=longest,
                    trailing_invalid_streak=streak.copy(),invalid_streak_limit=self.invalid_streak_limit,
                    averaging_policy='valid samples only')


class DispersionService:
    def __init__(self,backend,cancel):
        self.backend=backend;self.reader=QMOrbitReader(backend);self.reader.cancel=cancel;self.last_result=None
        self.successful_widths={}

    def check(self):self.reader.check_cancel()

    def signal(self,opt):
        self.check()
        o=Options(plane='x',bpm_samples=10,frequency_change_khz=opt.frequency_change_khz,
                  momentum_compaction=opt.momentum_compaction)
        _,valid=self.reader._signal('dispersion',o)
        nominal,minus,plus=self.reader.orbit_measurements[-3:]
        def variance(frame,axis):
            count=np.asarray(frame['valid_sample_count'])
            # Stored std uses ddof=0; std^2/(n-1) is the unbiased estimate of mean variance.
            return np.divide(np.asarray(frame['std'+axis])**2,count-1,
                             out=np.full(count.shape,np.nan),where=count>1)
        dp=opt.frequency_change_khz*1000/(self.backend.nominal_frequency_hz*opt.momentum_compaction)
        self.signal_variance=np.array([(variance(minus,a)+variance(plus,a))/dp**2 for a in ('x','y')]+
                                      [variance(nominal,a) for a in ('x','y')])
        if not opt.measure_tunes:
            return self.reader.full_signal.copy(),valid,None,dict(source='skipped',readback_update_verified=False,
                acquisition_quality='Tune acquisition and tune constraints disabled by option')
        tunes=self.backend.measure_tunes(cancel=self.reader.cancel)
        q=np.asarray(tunes['tunes'],float)
        if q.shape!=(2,) or not np.isfinite(q).all():raise ValueError('Invalid tune measurement')
        return self.reader.full_signal.copy(),valid,q,tunes

    def validity_report(self,mask,previous,enabled,straight,start,label,opt):
        """Describe the intersection and the consecutive-invalid update rule."""
        region=enabled&straight;common=previous&mask&region
        lost=[n for n,drop in zip(self.backend.bpm_names,previous&~mask&region) if drop]
        record=dict(label=label,enabled_straight=int(region.sum()),current_straight=int((mask&region).sum()),
                    previous_common=int((previous&region).sum()),common_straight=int(common.sum()),
                    lost_bpms=lost,settings=[])
        labels=('nominal RF',f'RF -{opt.frequency_change_khz/2:g} kHz',f'RF +{opt.frequency_change_khz/2:g} kHz')
        for index,frame in enumerate(self.reader.orbit_measurements[start:],start):
            samples=frame['samples'];valid=np.asarray(samples['valid'],bool)[:,region]
            x=np.asarray(samples['x'])[:,region];y=np.asarray(samples['y'])[:,region];q=np.asarray(samples['tmit'])[:,region]
            finite=np.isfinite(x)&np.isfinite(y);intensity=np.isfinite(q)&(q>0)
            counts=valid.sum(axis=1)
            record['settings'].append(dict(label=labels[index-start],measurement_index=index,
                valid_straight=int((np.asarray(frame['valid'],bool)&region).sum()),
                per_sample_valid_straight=counts.tolist(),nonfinite_xy=int((~finite).sum()),
                invalid_intensity=int((~intensity).sum()),other_invalid=int((~valid&finite&intensity).sum()),
                valid_sample_count=np.asarray(frame['valid_sample_count'])[region].tolist(),
                max_invalid_streak=np.asarray(frame['max_invalid_streak'])[region].tolist(),
                invalid_streak_limit=frame['invalid_streak_limit'],
                source_diagnostics=copy.deepcopy(frame.get('validity_diagnostics',[]))))
        settings='; '.join(f"{s['label']}: valid={s['valid_straight']}, "
            f"per-update={s['per_sample_valid_straight']}, nonfinite XY={s['nonfinite_xy']}, "
            f"invalid intensity={s['invalid_intensity']}, other/status invalid={s['other_invalid']}, "
            f"streak >= {s['invalid_streak_limit']}: {sum(n>=s['invalid_streak_limit'] for n in s['max_invalid_streak'])} BPMs" for s in record['settings'])
        record['message']=(f"BPM validity: {label}; enabled straight={record['enabled_straight']}, "
            f"this observation={record['current_straight']}, common={record['previous_common']} -> {record['common_straight']}. "
            +settings+'. Lost: '+(', '.join(lost) or 'none'))
        return record

    def profile_signature(self):
        encoded=json.dumps(serial(getattr(self.backend,'profile',{})),sort_keys=True,allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def checkpoint(self,data,path,event):
        data['orbit_measurements']=copy.deepcopy(self.reader.orbit_measurements)
        data['successful_probe_widths']=copy.deepcopy(self.successful_widths)
        if path is None:return
        data['checkpoint_file']=str(path)
        data['checkpoint_saved_at']=datetime.now(timezone.utc).isoformat()
        save_checkpoint(path,data)
        event(dict(kind='checkpoint',path=str(path),completed=data['scan_state']['completed'],total=len(data['actuators']),
                   skipped=len(data.get('skipped_qms',[]))))

    def width_context(self,data,opt):
        return dict(backend_id=data['backend_id'],profile_signature=data['profile_signature'],names=list(data['names']),
            frequency_change_khz=opt.frequency_change_khz,momentum_compaction=opt.momentum_compaction,
            excluded_bpms=sorted(opt.excluded_bpms),initial_probe_A=opt.probe_A,current_limit_A=opt.probe_current_limit_A,
            measure_tunes=opt.measure_tunes,constrain_cod=opt.constrain_cod,orbit_tolerance_mm=opt.orbit_tolerance_mm)

    def restore_width_cache(self,data):
        for n,entry in data.get('successful_probe_widths',{}).items():
            if n not in QM_CANDIDATES or not isinstance(entry,dict):continue
            width=entry.get('width_A')
            if isinstance(width,(int,float)) and np.isfinite(width) and width>=PROBE_INCREMENT_A and 'context' in entry and 'baseline' in entry:
                self.successful_widths[n]=copy.deepcopy(entry)

    def initial_width(self,n,data,opt):
        entry=self.successful_widths.get(n)
        if opt.adaptive_probe and opt.reuse_probe_widths and entry and entry['context']==self.width_context(data,opt):
            if self.backend.same_settings(data['baseline'],entry['baseline']):return probe_floor(entry['width_A']),'previous response S/N estimate'
        return opt.probe_A,'initial setting'

    def probe_quality(self,frames,variances,mask,opt):
        dx=(frames[1]-frames[0])[0,mask];noise2=(variances[1]+variances[0])[0,mask]
        signal=float(np.sqrt(np.mean(dx**2)));noise=float(np.sqrt(np.mean(noise2)))
        if not np.isfinite([signal,noise]).all():raise ValueError('Invalid QM response noise estimate')
        dp=opt.frequency_change_khz*1000/(self.backend.nominal_frequency_hz*opt.momentum_compaction)
        return dict(signal_rms=signal,noise_rms=noise,snr=signal/noise if noise>0 else None,
            target_snr=opt.target_snr,usable=bool(signal>1.e-12 and signal>=opt.target_snr*noise),
            orbit_response_rms_mm=signal*abs(dp),orbit_noise_rms_mm=noise*abs(dp),
            target_change_mm=opt.target_snr*noise*abs(dp),bpm_count=int(mask.sum()),
            definition='RMS straight Dx change between the two measured QM currents; uncertainty from valid-sample SEM at RF +/- offsets')

    def calculate(self,opt,progress=lambda *_:None,event=lambda *_:None,checkpoint_path=None):
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
        for j,(n,c) in enumerate(zip(names,centers)):
            try:targets,_=probe_pair(n,c,limits[:,j],opt,opt.probe_A)
            except ProbeRangeUnavailable:continue
            b.validate_quadrupole_targets(n,[targets[0],c,targets[1]])
        if checkpoint_path is not None:prepare_destination(checkpoint_path)
        baseline=b.snapshot();self.reader.reset_sampling()
        measured,valid,tunes,tune_evidence=self.signal(opt)
        data=dict(schema='dr-qm-dispersion-1',mode='dispersion',backend_id=b.identity,
                  names=list(b.bpm_names),positions_m=np.asarray(b.positions_m),actuators=names,
                  centers=centers,limits=limits,baseline=baseline,full_measured=measured,valid=valid&enabled,
                  straight=straight,initial_tunes=tunes,initial_tune_evidence=tune_evidence,options=asdict(opt),applied=False,
                  full_signal_labels=['Dispersion X','Dispersion Y','COD X','COD Y'],scan_tunes=[],probe_readbacks=[],
                  tune_measurement_enabled=opt.measure_tunes,acquisition_options=asdict(opt),validity_history=[],
                  bpm_invalid_streak_limit=self.reader.invalid_streak_limit,
                  checkpoint_schema='dr-qm-scan-1',profile_signature=self.profile_signature(),
                  created_at=datetime.now(timezone.utc).isoformat(),probe_quality=[],probe_attempts=[],skipped_qms=[],
                  scan_state=dict(completed=0,columns=[],tune_columns=[],common_valid=valid.copy(),
                                  status='running',active_magnet=None))
        self.last_result=data;event(dict(kind='dispersion',data=copy.deepcopy(data)))
        diagnostic=self.validity_report(valid,np.ones_like(valid),enabled,straight,0,'Initial baseline',opt)
        data['validity_history'].append(diagnostic);progress(0,diagnostic['message'])
        return self.measure_remaining(data,opt,progress,event,checkpoint_path)

    def measure_remaining(self,data,opt,progress,event,checkpoint_path):
        b=self.backend;names=data['actuators'];centers=np.asarray(data['centers']);limits=np.asarray(data['limits'])
        enabled=np.array([n not in opt.excluded_bpms for n in data['names']]);straight=np.asarray(data['straight'],bool)
        state=data['scan_state'];valid=np.asarray(state['common_valid'],bool).copy()
        columns=[np.asarray(c,float) for c in state['columns']];qcolumns=[np.asarray(c,float) for c in state['tune_columns']]
        data['options']=asdict(opt);state['status']='running'
        try:
            if not np.any(valid&enabled&straight):
                raise ValueError('No valid straight-section BPM before QM probes. '+data['validity_history'][-1]['message'])
            self.checkpoint(data,checkpoint_path,event)
            for j in range(state['completed'],len(names)):
                self.check();n=names[j];c=centers[j]
                h,width_source=self.initial_width(n,data,opt)
                progress(round(90*j/len(names)),f'QM probe: {n} starting width {h:g} A ({width_source})')
                characterize=opt.adaptive_probe and opt.characterize_first_scan and width_source=='initial setting'
                state['active_magnet']=n;skip_reason=None;failed_width=None
                for attempt in range(opt.maximum_probe_pairs if opt.adaptive_probe else 1):
                    self.check()
                    limits[:,j]=quad_limits(b,n);data['limits']=limits.copy()
                    # Read parameter.A/B again before each pair; the live backend also validates each write.
                    try:targets,margin=probe_pair(n,c,limits[:,j],opt,h)
                    except ProbeRangeUnavailable as exc:
                        skip_reason=str(exc);break
                    frames=[];qs=[];variances=[];readbacks=[];pair_valid=valid.copy()
                    state['status']='probing';data['restored']=False
                    self.checkpoint(data,checkpoint_path,event)
                    for target in (targets[0],c,targets[1]):b.validate_quadrupole_targets(n,[target])
                    try:
                        try:
                            for target in targets:
                                self.check();b.set_quadrupole_current(n,float(target))
                                reading=float(b.quadrupole_current(n))
                                if not np.isfinite(reading):raise ValueError('Invalid QM current readback: '+n)
                                readbacks.append(reading);start=len(self.reader.orbit_measurements)
                                frame,mask,q,evidence=self.signal(opt)
                                diagnostic=self.validity_report(mask,pair_valid,enabled,straight,start,
                                    f'{n} probe {target-c:+g} A (readback {reading:+g} A)',opt)
                                data['validity_history'].append(diagnostic);pair_valid &= mask;data['valid']=pair_valid&enabled
                                progress(round(90*j/len(names)),diagnostic['message'])
                                if not np.any(pair_valid&enabled&straight):
                                    raise ProbeSignalRejected('Lost all common valid straight-section BPMs. '+diagnostic['message']+
                                        '. A BPM is excluded after 5 consecutive invalid updates; averages use valid samples only.')
                                if opt.adaptive_probe and np.any(valid&enabled&~mask):
                                    raise ProbeSignalRejected('New invalid BPMs during adaptive QM probe. '+diagnostic['message'])
                                if opt.constrain_cod and np.max(abs(frame[2:,pair_valid&enabled]-np.asarray(data['full_measured'])[2:,pair_valid&enabled]))>opt.orbit_tolerance_mm:
                                    raise ProbeSignalRejected('Measured COD change exceeds QM probe tolerance; restored')
                                frames.append(frame);variances.append(self.signal_variance.copy())
                                if opt.measure_tunes:
                                    qs.append(q);data['scan_tunes'].append(dict(magnet=n,current_A=target,measurement=evidence))
                        finally:
                            try:restore_quads(b,[n],[c]);data['restored']=True
                            except BaseException as exc:data['restoration_error']=str(exc);raise
                    except ProbeSignalRejected as exc:
                        if not opt.adaptive_probe:raise
                        self.successful_widths.pop(n,None)
                        failed_width=min(failed_width,h) if failed_width is not None else h
                        smaller=probe_floor(h/2)
                        data['probe_attempts'].append(dict(magnet=n,half_span_A=h,requested=targets.copy(),actual=readbacks,
                            accepted=False,rejected=True,reason=str(exc),next_width_A=smaller if smaller>=PROBE_INCREMENT_A else None))
                        if smaller<PROBE_INCREMENT_A or attempt+1==opt.maximum_probe_pairs:
                            skip_reason=f'{n}: no valid probe within the 0.01 A minimum / trial limit. '+str(exc);break
                        progress(round(90*j/len(names)),f'QM probe: {n} rejected at {h:g} A; restored, retry at {smaller:g} A')
                        h=smaller;continue
                    measured_span=readbacks[1]-readbacks[0]
                    if measured_span<=max(1.e-6,(targets[1]-targets[0])*.1):
                        skip_reason=f'Unresolved actual QM current span: {n}; requested={targets.tolist()}, readbacks={readbacks}'
                        data['probe_attempts'].append(dict(magnet=n,half_span_A=h,requested=targets.copy(),actual=readbacks,
                            accepted=False,reason=skip_reason,signal_frames=frames,signal_variances=variances))
                        break
                    quality=self.probe_quality(frames,variances,pair_valid&enabled&straight,opt)
                    next_width=next_probe_width(h,c,limits[:,j],measured_span,quality,opt,failed_width) if opt.adaptive_probe and not quality['usable'] else None
                    finished=(not opt.adaptive_probe or characterize or quality['usable'] or next_width is None or attempt+1==opt.maximum_probe_pairs)
                    data['probe_attempts'].append(dict(magnet=n,half_span_A=h,requested=targets.copy(),actual=readbacks,
                        quality=quality,accepted=finished,signal_frames=frames,signal_variances=variances,
                        next_width_A=next_width if not finished else None))
                    progress(round(90*j/len(names)),f"QM probe: {n} [{targets[0]:+g}, {targets[1]:+g}] A; response {quality['orbit_response_rms_mm']*1000:.3g} um / "
                        f"target {quality['target_change_mm']*1000:.3g} um; "+('accepted' if quality['usable'] else 'below target'))
                    if finished:break
                    self.checkpoint(data,checkpoint_path,event)
                    h=next_width
                if skip_reason is not None:
                    # Alignment placeholders only: explicitly unmeasured and always excluded from the solver.
                    columns.append(np.zeros(4*len(valid)))
                    if opt.measure_tunes:qcolumns.append(np.zeros(2))
                    data.setdefault('skipped_qms',[]).append(dict(magnet=n,reason=skip_reason))
                    data['probe_quality'].append(dict(skipped=True,measured=False,usable=False,reason=skip_reason,
                        signal_rms=None,noise_rms=None,snr=None,orbit_response_rms_mm=None,orbit_noise_rms_mm=None,target_change_mm=None))
                    data['probe_readbacks'].append(dict(magnet=n,requested=[],actual=[],skipped=True,reason=skip_reason,scheme='skipped'))
                    progress(round(90*(j+1)/len(names)),'SKIP QM probe: '+skip_reason)
                else:
                    valid=pair_valid
                    columns.append(((frames[1]-frames[0])/measured_span).ravel())
                    if opt.measure_tunes:qcolumns.append(((qs[1]-qs[0]+.5)%1-.5)/measured_span)
                    data['probe_quality'].append(quality)
                    scheme=('shifted inside probe range' if c<targets[0] or c>targets[1] else
                            'symmetric' if np.isclose(c-targets[0],targets[1]-c,rtol=0,atol=1.e-12) else 'asymmetric / one-sided')
                    data['probe_readbacks'].append(dict(magnet=n,requested=targets.copy(),actual=readbacks,scheme=scheme))
                    if quality['signal_rms']>1.e-12:
                        estimate=estimated_probe_width(c,limits[:,j],measured_span,quality,opt)
                        estimate=h if estimate is None else min(estimate,probe_floor(h*opt.probe_max_growth))
                        if quality['usable']:estimate=min(estimate,h)
                        if failed_width is not None:estimate=min(estimate,round(failed_width-PROBE_INCREMENT_A,12))
                        self.successful_widths[n]=dict(width_A=estimate,measured_width_A=h,quality=copy.deepcopy(quality),
                            context=self.width_context(data,opt),baseline=copy.deepcopy(data['baseline']))
                    else:self.successful_widths.pop(n,None)
                state.update(completed=j+1,columns=columns.copy(),tune_columns=qcolumns.copy(),common_valid=valid.copy(),
                             active_magnet=None,status='running')
                self.checkpoint(data,checkpoint_path,event)
                progress(round(90*(j+1)/len(names)),f'QM dispersion response {n} ({j+1}/{len(names)})')
            self.check()
            if not b.same_settings(b.snapshot(),data['baseline']):raise ValueError('Settings changed during QM response scan; calculate again')
            data.update(response=np.column_stack(columns),tune_response=np.column_stack(qcolumns) if opt.measure_tunes else None,
                        acquisition_valid=valid.copy(),valid=valid&enabled,
                        response_column_measured=[not r.get('skipped',False) for r in data['probe_readbacks']],
                        orbit_measurements=copy.deepcopy(self.reader.orbit_measurements))
            try:self.solve(data,opt)
            except InsufficientProbeQuality as exc:
                if not opt.characterize_first_scan:raise
                data['proposal_unavailable_reason']=str(exc)+'; response calibration saved. CALC again to use estimated widths.'
            state['status']='complete';data.pop('restored',None)
            self.checkpoint(data,checkpoint_path,event);event(dict(kind='dispersion',data=copy.deepcopy(data)))
            progress(100,data.get('proposal_unavailable_reason','QM proposal ready; all diagnostic currents and RF restored'))
            return data
        except BaseException as exc:
            data['error']=str(exc);state['status']='interrupted'
            try:data['restored']=bool(b.same_settings(b.snapshot(),data['baseline']))
            except Exception as restoration_error:data['restoration_error']=str(restoration_error);data['restored']=False
            try:self.checkpoint(data,checkpoint_path,event)
            except Exception as save_error:data['checkpoint_error']=str(save_error)
            raise

    def validate_checkpoint(self,data):
        """Validate stored columns before offering resume; performs no hardware IO."""
        if data.get('checkpoint_schema')!='dr-qm-scan-1':raise ValueError('No resumable QM scan state in this file')
        try:
            state=data['scan_state'];names=data['actuators'];n=len(data['names']);m=len(names);done=state['completed']
            if type(done) is not int or not 0<=done<=m or not m or len(set(names))!=m or not set(names)<=set(QM_CANDIDATES):
                raise ValueError('Invalid completed QM count / names')
            if state['status'] not in ('running','probing','interrupted','complete'):raise ValueError('Invalid scan status')
            if len(state['columns'])!=done or len(data['probe_quality'])!=done or len(data['probe_readbacks'])!=done:
                raise ValueError('Incomplete committed QM columns')
            for column in state['columns']:
                if np.asarray(column,float).shape!=(4*n,):raise ValueError('Invalid response column shape')
            for key,shape in [('full_measured',(4,n)),('centers',(m,)),('limits',(2,m)),('straight',(n,)),('valid',(n,))]:
                if np.asarray(data[key]).shape!=shape:raise ValueError('Invalid checkpoint '+key)
            if np.asarray(state['common_valid']).shape!=(n,):raise ValueError('Invalid checkpoint BPM mask')
            if not np.isfinite(np.r_[np.asarray(data['centers']),np.asarray(data['limits']).ravel()]).all():
                raise ValueError('Nonfinite checkpoint currents / limits')
            acquired=DispersionOptions(**data['acquisition_options']);acquired.validate()
            if len(state['tune_columns'])!=(done if acquired.measure_tunes else 0):raise ValueError('Incomplete tune response')
            if any(np.asarray(column).shape!=(2,) for column in state['tune_columns']):raise ValueError('Invalid tune response shape')
            measured=sum(not r.get('skipped') for r in data['probe_readbacks'])
            if len(data['orbit_measurements'])<3+6*measured:raise ValueError('Missing raw measurements')
            if state['status']=='complete' and done!=m:raise ValueError('Incomplete scan marked complete')
        except (KeyError,TypeError,IndexError) as exc:raise ValueError('Malformed QM scan checkpoint: '+str(exc)) from exc
        return data

    def resume(self,saved,opt,progress=lambda *_:None,event=lambda *_:None,checkpoint_path=None):
        opt.validate();self.check();self.validate_checkpoint(saved)
        self.restore_width_cache(saved)
        if saved.get('applied'):raise ValueError('Cannot resume an applied QM scan')
        b=self.backend;acquired=saved['acquisition_options'];names=saved['actuators']
        if saved['backend_id']!=b.identity or saved['names']!=list(b.bpm_names) or saved['profile_signature']!=self.profile_signature():
            raise ValueError('Machine / BPM order / hardware profile differs from the saved scan')
        for key in ('frequency_change_khz','momentum_compaction','measure_tunes'):
            if getattr(opt,key)!=acquired[key]:raise ValueError(key+' differs from the saved acquisition; restore the option or CALC again')
        if names!=[n for n in QM_CANDIDATES if n not in opt.fixed_actuators]:
            raise ValueError('QM selection differs from the saved scan; restore the selection or CALC again')
        if saved.get('bpm_invalid_streak_limit')!=self.reader.invalid_streak_limit:
            raise ValueError('BPM validity policy differs from the saved scan; CALC again')
        b.validate_mode('dispersion')
        if opt.measure_tunes and hasattr(b,'validate_tune_measurement'):b.validate_tune_measurement()
        if not b.same_settings(b.snapshot(),saved['baseline']):
            raise ValueError('Settings differ from the scan baseline. Restore original QM currents / RF and other changed settings before Resume.')
        centers=np.asarray(saved['centers'],float)
        tolerance=getattr(b,'profile',{}).get('current_tolerance_A',1.e-6)
        if not np.allclose([b.quadrupole_current(n) for n in names],centers,rtol=0,atol=tolerance):
            raise ValueError('QM currents differ from the saved scan baseline')
        limits=np.asarray([quad_limits(b,n) for n in names]).T
        if not np.array_equal(limits,np.asarray(saved['limits'])):raise ValueError('QM supply limits changed; CALC again')
        if checkpoint_path is not None:prepare_destination(checkpoint_path)
        data=copy.deepcopy(saved);self.last_result=data
        data['resumed_from']=saved.get('checkpoint_file');data.setdefault('initial_scan_measured',copy.deepcopy(data['full_measured']))
        self.reader.reset_sampling();self.reader.orbit_measurements=copy.deepcopy(data['orbit_measurements'])
        for key in ('error','restoration_error','checkpoint_error','delta','full_predicted','full_target'):
            data.pop(key,None)
        try:
            progress(0,f"Resume: {data['scan_state']['completed']}/{len(names)} QM responses retained; remeasuring baseline")
            frame,mask,tunes,evidence=self.signal(opt)
            valid=np.asarray(data['scan_state']['common_valid'],bool)&mask
            enabled=np.array([n not in opt.excluded_bpms for n in data['names']])
            if not np.any(valid&enabled&np.asarray(data['straight'],bool)):raise ValueError('No common valid straight BPM at resume baseline')
            data.setdefault('resume_checks',[]).append(dict(timestamp=datetime.now(timezone.utc).isoformat(),
                full_measured=frame,valid=mask,tunes=tunes,tune_evidence=evidence,options=asdict(opt),
                change_from_previous=frame-np.asarray(data['full_measured']),completed=data['scan_state']['completed']))
            # Use the current baseline as the fit target; retained columns were measured at the same magnet/RF settings.
            data.update(full_measured=frame,initial_tunes=tunes,initial_tune_evidence=evidence,valid=valid&enabled,restored=True)
            data['scan_state']['common_valid']=valid.copy()
        except BaseException as exc:
            data['error']=str(exc);data['scan_state']['status']='interrupted'
            try:self.checkpoint(data,checkpoint_path,event)
            except Exception as save_error:data['checkpoint_error']=str(save_error)
            raise
        return self.measure_remaining(data,opt,progress,event,checkpoint_path)

    def recalculate(self,data,opt,progress=lambda *_:None):
        """Refit a complete response without reading or writing any machine setting."""
        opt.validate();self.check()
        if 'response' not in data:raise ValueError('No complete measured response; CALC first')
        if data.get('applied'):raise ValueError('Restore original QM currents or CALC again before recalculating')
        if data.get('restoration_error') or (data.get('error') and not data.get('restored')):
            raise ValueError('Resolve the previous acquisition / restoration failure and CALC again')
        acquired=data.get('acquisition_options',data['options'])
        for key in ('frequency_change_khz','momentum_compaction'):
            if getattr(opt,key)!=acquired[key]:raise ValueError('RF / momentum settings changed; CALC again')
        expected=[n for n in QM_CANDIDATES if n not in opt.fixed_actuators]
        if expected!=list(data['actuators']):raise ValueError('QM selection changed; CALC again')
        result=copy.deepcopy(data)
        for key in ('error','restoration_error','restored','full_after','after_valid','after_tunes',
                    'after_tune_evidence','after_orbit_measurements','tune_check_performed','applied_currents_A','autosave'):
            result.pop(key,None)
        result.setdefault('acquisition_options',copy.deepcopy(acquired))
        result.update(applied=False,response_reused=True,recalculation_count=data.get('recalculation_count',0)+1)
        self.solve(result,opt);self.last_result=result
        progress(100,'Recalculated from measured response; no BPM acquisition or magnet / RF writes')
        return result

    def solve(self,data,opt):
        opt.validate();valid=np.asarray(data.get('acquisition_valid',data['valid']),bool).copy()
        valid &= np.array([n not in opt.excluded_bpms for n in data['names']])
        straight=np.asarray(data['straight'],bool)
        if not np.any(valid&straight):raise ValueError('No common valid straight-section BPM across scan')
        measured=np.asarray(data['full_measured']);target=measured.copy();target[0,straight]=0.
        response=np.asarray(data['response']);mask=np.tile(valid,4)
        weights=np.repeat([opt.dispersion_weight,opt.dispersion_weight,1.,1.],len(valid))
        a=(response*weights[:,None])[mask];residual=((measured-target).ravel()*weights)[mask]
        c=np.asarray(data['centers']);lo,hi=np.asarray(data['limits'])
        lower=np.maximum(lo,-opt.probe_current_limit_A)-c;upper=np.minimum(hi,opt.probe_current_limit_A)-c
        constraints=response[2*len(valid):][np.tile(valid,2)] if opt.constrain_cod else np.empty((0,len(c)))
        tolerance=np.full(len(constraints),opt.orbit_tolerance_mm)
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
                bounds=list(zip(lower[indices],upper[indices])),constraints=LinearConstraint(cm,-1.,1.) if len(cm) else (),
                method='SLSQP',options=dict(ftol=1.e-12,maxiter=500))
            if not result.success or (len(cm) and np.max(abs(cm@result.x))>1+1.e-6):raise ValueError('Bounded QM solve failed: '+result.message)
            return result.x,float(np.linalg.norm(matrix@result.x+residual)),s
        skipped={i for i,r in enumerate(data.get('probe_readbacks',[])) if r.get('skipped')}
        available=[i for i in range(len(c)) if i not in skipped and lower[i]<=upper[i]];selected=[]
        if not available:
            raise ValueError('No measurable QM response after automatic range adjustment. '+
                             '; '.join(r['reason'] for r in data.get('skipped_qms',[])))
        if opt.adaptive_probe and data.get('probe_quality'):
            available=[i for i in available if data['probe_quality'][i]['signal_rms']>1.e-12 and
                       data['probe_quality'][i]['signal_rms']>=opt.target_snr*data['probe_quality'][i]['noise_rms']]
        if not available:raise InsufficientProbeQuality('No QM response reaches the target signal/noise ratio; review probe quality / limits')
        if opt.selection=='all':selected=available;step,_,singular=fit(selected)
        else:
            score=float(np.linalg.norm(residual));step=np.zeros(0);singular=np.zeros(0)
            for _ in range(min(opt.maximum,len(available))):
                choices=[]
                for j in available:
                    x,norm,s=fit(selected+[j]);choices.append((norm,j,x,s))
                best=min(choices,key=lambda row:row[0])
                if best[0]>=score-1.e-12:break
                score,j,step,singular=best;selected.append(j);available.remove(j)
        delta=np.zeros(len(c));delta[selected]=step
        data.update(options=asdict(opt),valid=valid,delta=delta,full_target=target,full_predicted=measured+(response@delta).reshape(measured.shape),
                    predicted_tune_change=tune_response@delta if opt.measure_tunes else None,selected=[data['actuators'][i] for i in selected],
                    singular_values=singular,solver='bounded regularized SVD; '+
                    ('COD constraints; ' if opt.constrain_cod else 'COD hard limit disabled; ')+
                    ('tune constraints; ' if opt.measure_tunes else 'tune acquisition / tune constraints skipped; ')+opt.selection)
        return data

    def apply(self,data,progress=lambda *_:None,event=lambda *_:None):
        self.last_result=data
        self.check();b=self.backend;saved_options=dict(data['options']);saved_options.setdefault('measure_tunes',True)
        opt=DispersionOptions(**saved_options);b.validate_mode('dispersion')
        if data.get('applied') or data['backend_id']!=b.identity:raise ValueError('Stale or already applied QM proposal')
        if not b.same_settings(b.snapshot(),data['baseline']):raise ValueError('Settings changed since QM CALC')
        names=data['actuators'];centers=np.asarray(data['centers']);delta=np.asarray(data['delta']);changed=np.flatnonzero(abs(delta)>1.e-12)
        if not len(changed):raise ValueError('No QM correction proposed')
        if np.any(abs(centers[changed]+delta[changed])>opt.probe_current_limit_A+1.e-9):
            raise ValueError('QM proposal exceeds the shared absolute current limit; recalculate')
        for j in changed:b.validate_quadrupole_targets(names[j],[centers[j]+delta[j]])
        self.reader.reset_sampling()
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
            if opt.constrain_cod and np.max(abs(orbit_change))>opt.orbit_tolerance_mm:
                raise ValueError('Measured COD change exceeds QM correction tolerance; restored')
            straight=mask&np.asarray(data['straight'],bool)
            if np.linalg.norm(after[0,straight])>np.linalg.norm(np.asarray(data['full_measured'])[0,straight]):
                raise ValueError('Measured horizontal straight dispersion did not improve; restored')
            self.check();data['applied_currents_A']=[b.quadrupole_current(n) for n in names]
            data['applied']=True;data.pop('restored',None);event(dict(kind='dispersion',data=copy.deepcopy(data)))
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
        self.last_result=data;errors=[]
        skipped={r['magnet'] for r in data.get('probe_readbacks',[]) if r.get('skipped')}
        restore=[(n,c) for n,c in zip(data['actuators'],data['centers']) if n not in skipped]
        try:restore_quads(self.backend,[n for n,_ in restore],[c for _,c in restore])
        except Exception as exc:errors.append(str(exc))
        if data.get('checkpoint_schema'):
            try:self.backend.restore_frequency(data['baseline'].get('ramp_state',data['baseline']['frequency']))
            except Exception as exc:errors.append(str(exc))
        if errors:
            data.update(restored=False,restoration_error='; '.join(errors))
            raise RuntimeError(data['restoration_error'])
        data['applied']=False;data['restored']=True
        data.pop('restoration_error',None)
