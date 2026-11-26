"""Measured straight-section horizontal dispersion correction with QM trim or main supplies."""
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
import copy
import hashlib
import json
import re
import numpy as np
from scipy.optimize import minimize,LinearConstraint
from Knobs.DRCorrection.backends import MagnetReadbackError
from Knobs.DRCorrection.core import CorrectionService,Options,serial
from .model import name
from Knobs.DRCorrection.probe_quality import response_quality
from .qm_checkpoint import prepare_destination,save_checkpoint
from .hardware import QM_CANDIDATES,QM_MAIN_CANDIDATES,quadrupole_supply

PROBE_INCREMENT_A=.01
# Initial widths inferred from the 2026-11-19 13:58 DR scan, for S/N 6.5.
# These are probe widths, not absolute current targets or measured responses.
QM_PROBE_PRESETS_A={
    'QM1R.1':.16,'QM3R.1':.34,'QM4R.1':.13,'QM20R.1':.62,
    'QM21R.1':.50,'QM22R.1':.28,'QM23R.1':.16,
    'QM1R.2':.22,'QM3R.2':.21,'QM4R.2':.18,'QM20R.2':.47,
    'QM21R.2':.28,'QM22R.2':.20,'QM23R.2':.29,
}
PROBE_SNR_HEADROOM=1.3


@dataclass
class DispersionOptions:
    probe_A: float = .1
    use_probe_presets: bool = True
    adaptive_probe: bool = True
    probe_current_limit_A: float = 3.  # Legacy saved-file field; absolute limits come from the supply.
    target_snr: float = 5.
    maximum_probe_pairs: int = 12
    probe_max_growth: float = 3.
    reuse_probe_widths: bool = True
    characterize_first_scan: bool = False
    step_A: float = 3.  # Maximum |delta I| per Apply, relative to this iteration's baseline.
    frequency_change_khz: float = 4.
    momentum_compaction: float = .0021
    measure_tunes: bool = False
    tune_tolerance: float = .002
    constrain_cod: bool = False
    orbit_tolerance_mm: float = .05
    dispersion_weight: float = 1.
    orbit_weight: float = .1
    arc_weight: float = 0.
    arc_parity: str = 'odd'
    regularization: float = .01
    svd_cut: float = .04
    selection: str = 'all'
    maximum: int = 8
    excluded_bpms: tuple = ()
    fixed_actuators: tuple = None
    coil: str = 'trim'

    def __post_init__(self):
        if self.fixed_actuators is None:
            self.fixed_actuators=('QM2R.1','QM2R.2','QM3R.2') if self.coil=='trim' else ()

    @classmethod
    def from_saved(cls,options):
        # Older QM proposals used fixed weights for fields absent from their JSON.
        return cls(**{'dispersion_weight':.05,'orbit_weight':1.,'arc_weight':0.,**options})

    def actuators(self):
        candidates=QM_MAIN_CANDIDATES if self.coil=='main' else QM_CANDIDATES
        excluded={n.replace('_','.') for n in self.fixed_actuators}
        return [n for n in candidates if n not in excluded]

    def validate(self):
        if self.coil not in ('trim','main'):raise ValueError('Unknown QM coil')
        for k in ('probe_A','target_snr','step_A','frequency_change_khz','momentum_compaction','orbit_tolerance_mm'):
            if not np.isfinite(getattr(self,k)) or getattr(self,k)<=0:raise ValueError('Invalid '+k)
        weights=(self.dispersion_weight,self.orbit_weight,self.arc_weight)
        if not np.isfinite(weights).all() or min(weights)<0 or max(weights)==0:
            raise ValueError('At least one positive correction weight is required')
        if self.arc_parity not in ('odd','even'):raise ValueError('Choose odd or even arc BPM numbers')
        if not isinstance(self.measure_tunes,bool):raise ValueError('Invalid measure_tunes option')
        if not isinstance(self.constrain_cod,bool):raise ValueError('Invalid constrain_cod option')
        if not isinstance(self.adaptive_probe,bool) or int(self.maximum_probe_pairs)!=self.maximum_probe_pairs or not 1<=self.maximum_probe_pairs<=20:
            raise ValueError('Invalid adaptive QM probe options')
        if self.probe_A<PROBE_INCREMENT_A or not np.isclose(self.probe_A/PROBE_INCREMENT_A,round(self.probe_A/PROBE_INCREMENT_A),atol=1.e-9,rtol=0):
            raise ValueError('Initial QM probe width must be a multiple of 0.01 A, minimum 0.01 A')
        if not np.isfinite(self.probe_max_growth) or self.probe_max_growth<=1 or not isinstance(self.reuse_probe_widths,bool):
            raise ValueError('Invalid QM growth / width reuse options')
        if not isinstance(self.characterize_first_scan,bool):raise ValueError('Invalid first-pass option')
        if not isinstance(self.use_probe_presets,bool):raise ValueError('Invalid per-QM probe preset option')
        if self.measure_tunes and (not np.isfinite(self.tune_tolerance) or self.tune_tolerance<=0):raise ValueError('Invalid tune_tolerance')
        if (not np.isfinite([self.regularization,self.svd_cut]).all() or self.regularization<0 or
                not 0<=self.svd_cut<1 or self.selection not in ('all','greedy') or
                int(self.maximum)!=self.maximum or not 1<=self.maximum<=16):raise ValueError('Invalid dispersion solver options')


def mb_numbers(names):
    numbers=[]
    for bpm in names:
        match=re.fullmatch(r'MB(\d+)R',name(bpm))
        numbers.append(int(match[1]) if match else -1)
    return np.asarray(numbers,int)


def straight_bpms(names):
    n=mb_numbers(names)
    return ((19<=n)&(n<=31)) | ((66<=n)&(n<=80))


def arc_bpms(names,parity):
    n=mb_numbers(names)
    in_arc=((1<=n)&(n<=18)) | ((32<=n)&(n<=65)) | ((81<=n)&(n<=96))
    return in_arc & (n%2==int(parity=='odd'))


def qm_objective(data,opt,valid):
    measured=np.asarray(data['full_measured'],float)
    straight=np.asarray(data['straight'],bool)
    arc=arc_bpms(data['names'],opt.arc_parity)&valid
    target=measured.copy()
    target[0,straight]=0.
    weights=np.empty_like(measured)
    weights[:2]=opt.dispersion_weight
    weights[2:]=opt.orbit_weight
    if opt.arc_weight:
        if not arc.any():raise ValueError('No valid '+opt.arc_parity+' arc BPMs for the dispersion objective')
        target[0,arc]=0.
        # Match the scale of the straight RMS even when the two groups differ in size.
        weights[0,arc]=opt.arc_weight*np.sqrt(np.count_nonzero(straight&valid)/arc.sum())
    return target,weights,arc


def quad_limits(backend,n):
    p=quadrupole_supply(backend,n)
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


def probe_bounds(limits):
    lower,upper=map(float,limits)
    lo=probe_ceil(lower);hi=probe_floor(upper)
    if lo<lower:lo=round(lo+PROBE_INCREMENT_A,12)
    if hi>upper:hi=round(hi-PROBE_INCREMENT_A,12)
    return lo,hi


def probe_pair(magnet,center,limits,opt,width):
    """Move the probe reference inside the allowed range, then clip the endpoints."""
    if not np.isfinite(limits).all() or limits[0]>=limits[1]:
        raise ValueError(f'Invalid QM supply limits for {magnet}: {limits}')
    lower,upper=probe_bounds(limits)
    detail=(f'{magnet}: current={center:+.6g} A; supply=[{limits[0]:+.6g}, {limits[1]:+.6g}] A; '
            f'allowed probe endpoints=[{lower:+.6g}, {upper:+.6g}] A')
    if not limits[0]<=center<=limits[1]:
        raise ProbeRangeUnavailable('Original current is outside supply limits; cannot guarantee restoration. '+detail)
    if upper-lower<PROBE_INCREMENT_A-1.e-9:raise ProbeRangeUnavailable('No 0.01 A QM probe span within limits. '+detail)
    reference=float(np.clip(center,lower,upper))
    targets=np.array([max(lower,probe_floor(reference-width)),min(upper,probe_ceil(reference+width))])
    if targets[1]-targets[0]<PROBE_INCREMENT_A-1.e-9:raise ProbeRangeUnavailable('No 0.01 A QM probe span within limits. '+detail)
    return targets,max(reference-lower,upper-reference)


def probe_scheme(center,targets):
    low,high=targets
    if not low<=center<=high:return 'shifted inside probe range'
    if np.isclose(center-low,high-center,rtol=0,atol=1.e-12):return 'symmetric'
    return 'asymmetric / one-sided'


def estimated_probe_width(center,limits,measured_span,quality,opt):
    """Infer a target width from measured span and S/N, including clipped pairs."""
    lo,hi=probe_bounds(limits);reference=float(np.clip(center,lo,hi))
    near=min(reference-lo,hi-reference);far=max(reference-lo,hi-reference)
    snr=quality['snr']
    if snr is None or snr<=0:return None
    # A small headroom avoids repeated measurements just below the target.
    wanted_span=measured_span*PROBE_SNR_HEADROOM*opt.target_snr/snr
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
                  momentum_compaction=opt.momentum_compaction,excluded_bpms=opt.excluded_bpms)
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
        if data['scan_state']['status']=='complete':data.setdefault('matrix_measured_at',datetime.now(timezone.utc).isoformat())
        data['orbit_measurements']=copy.deepcopy(self.reader.orbit_measurements)
        data['successful_probe_widths']=copy.deepcopy(self.successful_widths)
        if path is None:return
        data['checkpoint_file']=str(path)
        data['checkpoint_saved_at']=datetime.now(timezone.utc).isoformat()
        save_checkpoint(path,data)
        event(dict(kind='checkpoint',path=str(path),completed=len(data['rf_results']) if 'rf_results' in data else data['scan_state']['completed'],total=len(data['actuators']),
                   skipped=len(data.get('skipped_qms',[]))))

    def width_context(self,data,opt):
        context=dict(backend_id=data['backend_id'],profile_signature=data['profile_signature'],names=list(data['names']),
            frequency_change_khz=opt.frequency_change_khz,momentum_compaction=opt.momentum_compaction,
            excluded_bpms=sorted(opt.excluded_bpms),initial_probe_A=opt.probe_A,probe_limits='supply',
            measure_tunes=opt.measure_tunes,constrain_cod=opt.constrain_cod,orbit_tolerance_mm=opt.orbit_tolerance_mm)
        if opt.coil=='main':context['coil']='main'
        elif opt.use_probe_presets:context['probe_presets']='20261119-snr6p5'
        return context

    def restore_width_cache(self,data):
        for n,entry in data.get('successful_probe_widths',{}).items():
            if n not in QM_CANDIDATES+QM_MAIN_CANDIDATES or not isinstance(entry,dict):continue
            width=entry.get('width_A')
            if isinstance(width,(int,float)) and np.isfinite(width) and width>=PROBE_INCREMENT_A and 'context' in entry and 'baseline' in entry:
                self.successful_widths[n]=copy.deepcopy(entry)

    def initial_width(self,n,data,opt):
        entry=self.successful_widths.get(n)
        if opt.adaptive_probe and opt.reuse_probe_widths and entry and entry['context']==self.width_context(data,opt):
            if self.backend.same_settings(data['baseline'],entry['baseline']):return probe_floor(entry['width_A']),'previous response S/N estimate'
        if opt.use_probe_presets and n in QM_PROBE_PRESETS_A:
            return QM_PROBE_PRESETS_A[n],'per-QM initial preset (2026-11-19, S/N 6.5)'
        return opt.probe_A,'initial setting'

    def probe_quality(self,frames,variances,mask,opt):
        dx=(frames[1]-frames[0])[0,mask];noise2=(variances[1]+variances[0])[0,mask]
        signal=float(np.sqrt(np.mean(dx**2)));noise=float(np.sqrt(np.mean(noise2)))
        return self.quality_record(signal,noise,int(mask.sum()),opt)

    def quality_record(self,signal,noise,bpms,opt):
        if not np.isfinite([signal,noise]).all():raise ValueError('Invalid QM response noise estimate')
        dp=opt.frequency_change_khz*1000/(self.backend.nominal_frequency_hz*opt.momentum_compaction)
        return dict(response_quality(signal,noise,opt.target_snr),
            orbit_response_rms_mm=signal*abs(dp),orbit_noise_rms_mm=noise*abs(dp),
            target_change_mm=opt.target_snr*noise*abs(dp),bpm_count=bpms,
            definition='RMS straight Dx change between the two measured QM currents; uncertainty from valid-sample SEM at RF +/- offsets')

    def remember_width(self,n,data,opt,h,center,limits,measured_span,quality,failed_width):
        if quality['signal_rms']<=1.e-12:
            self.successful_widths.pop(n,None)
            return
        estimate=estimated_probe_width(center,limits,measured_span,quality,opt)
        estimate=h if estimate is None else min(estimate,probe_floor(h*opt.probe_max_growth))
        if quality['usable']:estimate=min(estimate,h)
        if failed_width is not None:estimate=min(estimate,round(failed_width-PROBE_INCREMENT_A,12))
        self.successful_widths[n]=dict(width_A=estimate,measured_width_A=h,quality=copy.deepcopy(quality),
            context=self.width_context(data,opt),baseline=copy.deepcopy(data['baseline']))

    def settings_match(self,actual,expected,targets):
        compare=getattr(self.backend,'same_qm_settings',None)
        if compare is not None:return compare(actual,expected,targets)
        return self.backend.same_settings(actual,expected)

    def snapshot(self,names,include_other_mains=False):
        snapshot=self.backend.snapshot()
        for n in names:
            if n in QM_MAIN_CANDIDATES and n not in snapshot['quadrupoles']:
                snapshot['quadrupoles'][n]=self.backend.quadrupole_current(n)
        if include_other_mains:
            # Keep a trim response from being reused after a main supply changes.
            # An unavailable, unused main PV must not prevent the trim scan.
            for n in getattr(self.backend,'main_quadrupoles',{}):
                if n not in snapshot['quadrupoles']:
                    try:snapshot['quadrupoles'][n]=self.backend.quadrupole_current(n)
                    except MagnetReadbackError:continue
        return snapshot

    def calculate(self,opt,progress=lambda *_:None,event=lambda *_:None,checkpoint_path=None,*,group_rf=False):
        opt.validate();self.check();b=self.backend;b.validate_mode('dispersion')
        if opt.measure_tunes and hasattr(b,'validate_tune_measurement'):b.validate_tune_measurement()
        if not opt.measure_tunes:progress(0,'Tune acquisition / tune constraints skipped; measuring dispersion and COD')
        names=opt.actuators()
        supplies=getattr(b,'main_quadrupoles' if opt.coil=='main' else 'quadrupoles',{})
        missing=set(names)-set(supplies)
        if missing:raise ValueError(f'Confirmed QM {opt.coil} mapping / calibration missing: '+', '.join(sorted(missing)))
        if not names:raise ValueError(f'No enabled QM {opt.coil} supplies')
        enabled=np.array([n not in opt.excluded_bpms for n in b.bpm_names]);straight=straight_bpms(b.bpm_names)
        if not np.any(straight&enabled):raise ValueError('No enabled BPM in MB19R–MB31R / MB66R–MB80R')
        centers=np.array([b.quadrupole_current(n) for n in names]);limits=np.array([quad_limits(b,n) for n in names]).T
        if not np.isfinite(np.r_[centers,limits.ravel()]).all() or np.any(limits[0]>=limits[1]):raise ValueError('Invalid QM limits / current')
        for j,(n,c) in enumerate(zip(names,centers)):
            try:targets,_=probe_pair(n,c,limits[:,j],opt,opt.probe_A)
            except ProbeRangeUnavailable:continue
            b.validate_quadrupole_targets(n,[targets[0],c,targets[1]])
        if checkpoint_path is not None:prepare_destination(checkpoint_path)
        baseline=self.snapshot(names,include_other_mains=opt.coil=='trim');self.reader.reset_sampling()
        if group_rf and not opt.measure_tunes:
            valid=np.zeros(len(b.bpm_names),bool)
            measured=np.full((4,len(valid)),np.nan)
            tunes=None;tune_evidence=dict(source='skipped')
        else:measured,valid,tunes,tune_evidence=self.signal(opt)
        self.reader.sync_dispersion_selection('dispersion',opt)
        enabled=np.array([n not in opt.excluded_bpms for n in b.bpm_names])
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
        self.last_result=data
        if group_rf and not opt.measure_tunes:
            from .qm_rf_scan import measure_qms
            data['rf_blocks']=[]
            return measure_qms(self,data,opt,progress,event,checkpoint_path)
        event(dict(kind='dispersion',data=copy.deepcopy(data)))
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
                characterize=opt.adaptive_probe and opt.characterize_first_scan and width_source!='previous response S/N estimate'
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
                                self.reader.sync_dispersion_selection('dispersion',opt)
                                enabled=np.array([n not in opt.excluded_bpms for n in data['names']])
                                data['options']['excluded_bpms']=opt.excluded_bpms
                                diagnostic=self.validity_report(mask,pair_valid,enabled,straight,start,
                                    f'{n} probe {target-c:+g} A (readback {reading:+g} A)',opt)
                                data['validity_history'].append(diagnostic);pair_valid &= mask;data['valid']=pair_valid&enabled
                                progress(round(90*j/len(names)),diagnostic['message'])
                                if not np.any(pair_valid&enabled&straight):
                                    raise ProbeSignalRejected('Lost all common valid straight-section BPMs. '+diagnostic['message']+
                                        '. A BPM is excluded after 5 consecutive invalid updates; averages use valid samples only.')
                                if opt.adaptive_probe and np.any(valid&enabled&~mask):
                                    raise ProbeSignalRejected('New invalid BPMs during adaptive QM probe. '+diagnostic['message'])
                                if opt.constrain_cod and np.max(abs(frame[2:,pair_valid&enabled]-np.asarray(data['full_measured'],float)[2:,pair_valid&enabled]))>opt.orbit_tolerance_mm:
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
                    self.reader.sync_dispersion_selection('dispersion',opt)
                    enabled=np.array([n not in opt.excluded_bpms for n in data['names']])
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
                    data['probe_readbacks'].append(dict(magnet=n,requested=targets.copy(),actual=readbacks,scheme=probe_scheme(c,targets)))
                    self.remember_width(n,data,opt,h,c,limits[:,j],measured_span,quality,failed_width)
                state.update(completed=j+1,columns=columns.copy(),tune_columns=qcolumns.copy(),common_valid=valid.copy(),
                             active_magnet=None,status='running')
                self.checkpoint(data,checkpoint_path,event)
                progress(round(90*(j+1)/len(names)),f'QM dispersion response {n} ({j+1}/{len(names)})')
            self.check()
            if not self.settings_match(self.snapshot(data['baseline']['quadrupoles']),data['baseline'],names):raise ValueError('Settings changed during QM response scan; calculate again')
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
            try:data['restored']=bool(self.settings_match(self.snapshot(data['baseline']['quadrupoles']),data['baseline'],names))
            except Exception as restoration_error:data['restoration_error']=str(restoration_error);data['restored']=False
            try:self.checkpoint(data,checkpoint_path,event)
            except Exception as save_error:data['checkpoint_error']=str(save_error)
            raise

    def validate_checkpoint(self,data):
        """Validate stored columns before offering resume; performs no hardware IO."""
        if data.get('checkpoint_schema')!='dr-qm-scan-1':raise ValueError('No resumable QM scan state in this file')
        try:
            state=data['scan_state'];names=data['actuators'];n=len(data['names']);m=len(names);done=state['completed']
            acquired=DispersionOptions.from_saved(data['acquisition_options']);acquired.validate()
            if type(done) is not int or not 0<=done<=m or not m or names!=acquired.actuators() or acquired.coil!=data['options'].get('coil','trim'):
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
            if len(state['tune_columns'])!=(done if acquired.measure_tunes else 0):raise ValueError('Incomplete tune response')
            if any(np.asarray(column).shape!=(2,) for column in state['tune_columns']):raise ValueError('Invalid tune response shape')
            measured=sum(not r.get('skipped') for r in data['probe_readbacks'])
            if ('rf_blocks' not in data or measured) and len(data['orbit_measurements'])<3+6*measured:raise ValueError('Missing raw measurements')
            if 'rf_blocks' in data:
                from .qm_rf_scan import validate_rf_checkpoint
                validate_rf_checkpoint(data,DispersionOptions.from_saved(data['options']))
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
        if names!=opt.actuators():
            raise ValueError('QM selection differs from the saved scan; restore the selection or CALC again')
        if saved.get('bpm_invalid_streak_limit')!=self.reader.invalid_streak_limit:
            raise ValueError('BPM validity policy differs from the saved scan; CALC again')
        b.validate_mode('dispersion')
        if opt.measure_tunes and hasattr(b,'validate_tune_measurement'):b.validate_tune_measurement()
        if not self.settings_match(self.snapshot(saved['baseline']['quadrupoles']),saved['baseline'],names):
            raise ValueError('Settings differ from the scan baseline. Restore original QM currents / RF and other changed settings before Resume.')
        centers=np.asarray(saved['centers'],float)
        tolerance=getattr(b,'profile',{}).get('current_tolerance_A',1.e-6)
        if hasattr(b,'quadrupole_tolerance'):
            tolerance=np.array([b.quadrupole_tolerance(n,c) for n,c in zip(names,centers)])
        readings=np.array([b.quadrupole_current(n) for n in names])
        if not np.all((centers-tolerance<=readings)&(readings<=centers+tolerance)):
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
            if 'rf_blocks' in data and not np.any(data['scan_state']['common_valid']):valid=mask.copy()
            enabled=np.array([n not in opt.excluded_bpms for n in data['names']])
            if not np.any(valid&enabled&np.asarray(data['straight'],bool)):raise ValueError('No common valid straight BPM at resume baseline')
            data.setdefault('resume_checks',[]).append(dict(timestamp=datetime.now(timezone.utc).isoformat(),
                full_measured=frame,valid=mask,tunes=tunes,tune_evidence=evidence,options=asdict(opt),
                change_from_previous=frame-np.asarray(data['full_measured'],float),completed=data['scan_state']['completed']))
            # Use the current baseline as the fit target; retained columns were measured at the same magnet/RF settings.
            data.update(full_measured=frame,initial_tunes=tunes,initial_tune_evidence=evidence,valid=valid&enabled,restored=True)
            data['scan_state']['common_valid']=valid.copy()
        except BaseException as exc:
            data['error']=str(exc);data['scan_state']['status']='interrupted'
            try:self.checkpoint(data,checkpoint_path,event)
            except Exception as save_error:data['checkpoint_error']=str(save_error)
            raise
        if 'rf_blocks' in data:
            from .qm_rf_scan import measure_qms
            # A new probe definition invalidates unfinished RF pairs. Keep the
            # committed responses and re-plan only the outstanding magnets.
            if any(data['options'].get(k)!=getattr(opt,k) for k in (
                    'probe_A','target_snr','maximum_probe_pairs','use_probe_presets','adaptive_probe','constrain_cod',
                    'orbit_tolerance_mm','characterize_first_scan','reuse_probe_widths','probe_max_growth')):
                data.pop('rf_round',None)
                data['rf_pending'].clear()
            return measure_qms(self,data,opt,progress,event,checkpoint_path)
        return self.measure_remaining(data,opt,progress,event,checkpoint_path)

    def check_reuse_options(self,data,opt,*,current_beam=False):
        opt.validate();self.check()
        if 'response' not in data:raise ValueError('No complete measured response; CALC first')
        if data.get('restoration_error') or (data.get('error') and not data.get('restored')):
            raise ValueError('Resolve the previous acquisition / restoration failure and CALC again')
        acquired=data.get('acquisition_options',data['options'])
        # Dispersion columns are normalized per dp/p. A fresh observation can use a new RF span.
        if opt.momentum_compaction!=acquired['momentum_compaction'] or (not current_beam and opt.frequency_change_khz!=acquired['frequency_change_khz']):
            raise ValueError('RF / momentum settings differ from the measured response')
        expected=opt.actuators()
        matches=set(expected)<=set(data['actuators']) if current_beam else expected==list(data['actuators'])
        if not matches or acquired.get('coil','trim')!=opt.coil:
            raise ValueError('QM selection changed; CALC again')

    def recalculate(self,data,opt,progress=lambda *_:None):
        """Refit a complete response without reading or writing any machine setting."""
        self.check_reuse_options(data,opt)
        result=copy.deepcopy(data)
        if data.get('applied'):
            self._advance_iteration(result,opt)
        self.fit_reused_response(result,opt)
        progress(100,'Recalculated from measured response; no BPM acquisition or magnet / RF writes')
        return result

    def matrix_from_scan(self,data):
        """Keep calibration columns and their evidence, independently of the old proposal."""
        if not isinstance(data,dict) or data.get('schema')!='dr-qm-dispersion-1' or data.get('mode')!='dispersion':
            raise ValueError('Select a QM trim or main dispersion matrix')
        if not data.get('matrix_only'):self.validate_checkpoint(data)
        if data['scan_state']['status']!='complete':
            raise ValueError('Select a completed measured QM matrix')
        if data.get('restoration_error') or (data.get('error') and not data.get('restored')):
            raise ValueError('Unresolved error in the selected scan')
        acquired=DispersionOptions.from_saved(data['acquisition_options'])
        names=data['actuators'];n=len(data['names']);m=len(names)
        origin=data.get('response_origin') or data.get('iteration_origin',data)
        valid=np.asarray(origin.get('acquisition_valid',origin.get('valid',data['scan_state']['common_valid'])),bool)
        response=np.asarray(data['response'],float)
        if (names!=acquired.actuators() or not m or data['options'].get('coil','trim')!=acquired.coil
                or valid.shape!=(n,) or response.shape!=(4*n,m) or not np.isfinite(response[np.tile(valid,4)]).all()
                or len(data['probe_quality'])!=m or len(data['probe_readbacks'])!=m):
            raise ValueError('Invalid QM coil, columns or BPM mapping')
        keys=('schema','mode','backend_id','profile_signature','names','positions_m','actuators','acquisition_options',
              'response','probe_quality','probe_readbacks','skipped_qms','tune_response','full_signal_labels','created_at','matrix_measured_at')
        matrix={k:data[k] for k in keys if k in data}
        matrix.update(matrix_only=True,options=dict(data['acquisition_options']),straight=straight_bpms(data['names']),
                      valid=valid,scan_state=dict(status='complete',completed=m,common_valid=valid),
                      response_origin=dict(valid=valid,measured_at=origin.get('measured_at') or data.get('created_at')))
        return matrix

    def measure_current_dispersion(self,opt,*,before=None):
        """Measure the current beam without probing a QM or constructing a proposal."""
        self.check()
        b=self.backend;b.validate_mode('dispersion')
        names=opt.actuators()
        if opt.measure_tunes and hasattr(b,'validate_tune_measurement'):b.validate_tune_measurement()
        if before is None:before=self.snapshot(names,include_other_mains=opt.coil=='trim')
        self.reader.reset_sampling()
        measured,valid,tunes,evidence=self.signal(opt)
        self.check()
        baseline=self.snapshot(before['quadrupoles'])
        if not self.settings_match(baseline,before,names):
            raise ValueError('Machine settings changed during the dispersion measurement')
        if not (valid & ~np.isin(b.bpm_names,opt.excluded_bpms)).any():raise ValueError('No valid enabled BPM in dispersion measurement')
        return dict(baseline=baseline,full_measured=measured,valid=valid,tunes=tunes,evidence=evidence,
                    frames=self.reader.orbit_measurements.copy(),frequency_change_khz=opt.frequency_change_khz,
                    momentum_compaction=opt.momentum_compaction,measured_at=datetime.now(timezone.utc).isoformat(),
                    names=list(b.bpm_names),positions_m=b.positions_m,
                    full_signal_labels=['Dispersion X','Dispersion Y','COD X','COD Y'])

    def reuse_on_current_beam(self,data,opt,progress=lambda *_:None,*,observation=None,keep_iterations=False):
        """Measure the current baseline once, then solve with the stored QM response."""
        self.check_reuse_options(data,opt,current_beam=True)
        if not data.get('matrix_only'):self.validate_checkpoint(data)
        b=self.backend
        if data['scan_state']['status']!='complete':
            raise ValueError('A complete QM response matrix is required')
        if data['backend_id']!=b.identity or data['names']!=list(b.bpm_names) or data['profile_signature']!=self.profile_signature():
            raise ValueError('Machine, BPM order or hardware profile differs from the saved response')
        names=data['actuators']
        if np.shape(data['response'])!=(4*len(b.bpm_names),len(names)):
            raise ValueError('Invalid saved QM response dimensions')
        if opt.measure_tunes and np.shape(data.get('tune_response'))!=(2,len(names)):
            raise ValueError('No measured tune response in the saved matrix')
        baseline=self.snapshot(names,include_other_mains=opt.coil=='trim')
        if (observation is None or observation['frequency_change_khz']!=opt.frequency_change_khz
                or observation['momentum_compaction']!=opt.momentum_compaction
                or not self.settings_match(baseline,observation['baseline'],names)):
            progress(0,'Measuring current dispersion and COD')
            observation=self.measure_current_dispersion(opt,before=baseline)
            baseline=observation['baseline']
        measured=observation['full_measured'];valid=observation['valid']
        tunes=observation.get('tunes');evidence=observation.get('evidence')
        centers=np.array([baseline['quadrupoles'][n] for n in names],float)
        limits=np.array([quad_limits(b,n) for n in names],float).T
        if not np.isfinite(np.r_[centers,limits.ravel()]).all() or np.any(limits[0]>=limits[1]):
            raise ValueError('Invalid current QM limits / readbacks')

        result=copy.deepcopy(data)
        if 'response_origin' not in result:
            origin=data.get('iteration_origin',data)
            matrix_valid=origin.get('acquisition_valid',data.get('acquisition_valid',data['scan_state']['common_valid']))
            result['response_origin']=dict(baseline=copy.deepcopy(origin['baseline']),
                measured_at=data.get('created_at'),checkpoint_file=data.get('checkpoint_file'),valid=copy.deepcopy(matrix_valid))
        if keep_iterations and data.get('applied'):
            self._advance_iteration(result,opt)
        if not keep_iterations:
            for key in ('iteration_origin','iteration_history','iteration_valid','iteration_index'):
                result.pop(key,None)
        for key in ('initial_scan_measured','checkpoint_file','checkpoint_saved_at'):
            result.pop(key,None)
        result.update(baseline=baseline,centers=centers,limits=limits,full_measured=measured,
                      initial_tunes=tunes,initial_tune_evidence=evidence,baseline_valid=valid,
                      acquisition_valid=np.asarray(result['response_origin']['valid'],bool)&valid,
                      baseline_measured_at=observation['measured_at'],
                      baseline_orbit_measurements=observation['frames'])
        self.fit_reused_response(result,opt)
        progress(100,'Current dispersion measured. Review the QM proposal before Apply.')
        return result

    def fit_reused_response(self,result,opt):
        for key in ('error','restoration_error','restored','full_after','after_valid','after_tunes',
                    'after_tune_evidence','after_orbit_measurements','tune_check_performed','applied_currents_A',
                    'applied_baseline','applied_at','autosave'):
            result.pop(key,None)
        result.setdefault('acquisition_options',copy.deepcopy(result['options']))
        result.update(applied=False,response_reused=True,recalculation_count=result.get('recalculation_count',0)+1)
        self.solve(result,opt);self.last_result=result

    def _advance_iteration(self,data,opt):
        """Use the verified post-Apply observation, never the old residual or prediction."""
        if data.get('backend_id')!=self.backend.identity or data['names']!=list(self.backend.bpm_names):
            raise ValueError('Machine / BPM order differs from the applied QM correction')
        n=len(data['names']);m=len(data['actuators'])
        after=np.asarray(data.get('full_after'),float)
        valid=np.asarray(data.get('after_valid'),bool)
        currents=np.asarray(data.get('applied_currents_A'),float)
        if after.shape!=(4,n) or valid.shape!=(n,) or currents.shape!=(m,) or not np.isfinite(currents).all():
            raise ValueError('Missing verified after-Apply measurements / currents; CALC again')
        if opt.measure_tunes and (not data.get('tune_check_performed') or
                np.asarray(data.get('after_tunes')).shape!=(2,)):
            raise ValueError('No verified after-Apply tunes; CALC with tune acquisition enabled first')
        baseline=copy.deepcopy(data['baseline'])
        if not set(data['actuators'])<=set(baseline.get('quadrupoles',{})):
            raise ValueError('Missing QM settings in saved baseline; CALC again')
        baseline['quadrupoles'].update(zip(data['actuators'],currents.tolist()))
        # Older applied files have the measured currents but no complete post-Apply snapshot.
        # Keep all other settings at their saved values; Apply checks them against the machine.
        applied_baseline=data.get('applied_baseline',baseline)
        if not self.backend.same_settings(applied_baseline,baseline):
            raise ValueError('After-Apply settings disagree with the saved correction; CALC again')
        origin_keys=('baseline','centers','full_measured','valid','acquisition_valid',
                     'initial_tunes','initial_tune_evidence')
        data.setdefault('iteration_origin',{key:copy.deepcopy(data[key]) for key in origin_keys if key in data})
        history_keys=origin_keys+('delta','full_predicted','full_after','after_valid','after_tunes',
            'after_tune_evidence','after_orbit_measurements','applied_currents_A','applied_baseline',
            'applied_at','options','selected','autosave')
        data.setdefault('iteration_history',[]).append(
            dict(kind='applied',iteration=data.get('iteration_index',0)+1,
                 **{key:copy.deepcopy(data[key]) for key in history_keys if key in data}))
        data.update(baseline=copy.deepcopy(applied_baseline),centers=currents.copy(),full_measured=after.copy(),
                    initial_tunes=copy.deepcopy(data.get('after_tunes')),
                    initial_tune_evidence=copy.deepcopy(data.get('after_tune_evidence')),
                    iteration_valid=np.asarray(data.get('iteration_valid',np.ones(n,bool)),bool)&valid,
                    iteration_index=data.get('iteration_index',0)+1)

    def solve(self,data,opt):
        self.reader.sync_dispersion_selection('dispersion',opt)
        opt.validate();valid=np.asarray(data.get('acquisition_valid',data['valid']),bool).copy()
        valid &= np.asarray(data.get('iteration_valid',np.ones_like(valid)),bool)
        valid &= np.array([n not in opt.excluded_bpms for n in data['names']])
        straight=np.asarray(data['straight'],bool)
        if not np.any(valid&straight):raise ValueError('No common valid straight-section BPM across scan')
        # JSON stores unavailable BPM values as null; preserve them as NaN and
        # apply the recorded validity mask before checking the fitted rows.
        measured=np.asarray(data['full_measured'],float)
        target,weights,arc=qm_objective(data,opt,valid)
        response=np.asarray(data['response'],float);mask=np.tile(valid,4)
        a=(response*weights.reshape(-1,1))[mask];residual=((measured-target)*weights).ravel()[mask]
        c=np.asarray(data['centers']);lo,hi=np.asarray(data['limits'])
        lower=np.maximum(lo-c,-opt.step_A);upper=np.minimum(hi-c,opt.step_A)
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
        enabled=set(opt.actuators())
        available=[i for i,n in enumerate(data['actuators']) if n in enabled and i not in skipped and lower[i]<=upper[i]];selected=[]
        if not available:
            raise ValueError('No measurable QM response after automatic range adjustment. '+
                             '; '.join(r['reason'] for r in data.get('skipped_qms',[])))
        if opt.adaptive_probe and data.get('probe_quality'):
            available=[i for i in available if data['probe_quality'][i]['signal_rms']>1.e-12 and
                       data['probe_quality'][i]['signal_rms']>=opt.target_snr*data['probe_quality'][i]['noise_rms']]
        if not available:raise InsufficientProbeQuality('No QM response reaches the target signal/noise ratio; review probe quality / limits')
        if not np.any(residual):step=np.zeros(0);singular=np.zeros(0)
        elif opt.selection=='all':selected=available;step,_,singular=fit(selected)
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
        data['arc_bpms']=np.asarray(data['names'])[arc].tolist() if opt.arc_weight else []
        data.pop('proposal_unavailable_reason',None)
        return data

    def apply(self,data,progress=lambda *_:None,event=lambda *_:None):
        self.last_result=data
        self.check();b=self.backend;saved_options=dict(data['options']);saved_options.setdefault('measure_tunes',True)
        opt=DispersionOptions.from_saved(saved_options);opt.validate();b.validate_mode('dispersion')
        if data.get('applied') or data['backend_id']!=b.identity:raise ValueError('Stale or already applied QM proposal')
        names=data['actuators']
        enabled=set(opt.actuators())
        if not enabled<=set(names) or any(n not in enabled and abs(d)>1.e-12 for n,d in zip(names,data['delta'])):
            raise ValueError('QM selection differs from the measured response')
        if not self.settings_match(self.snapshot(data['baseline']['quadrupoles']),data['baseline'],names):raise ValueError('Settings changed since QM CALC')
        centers=np.asarray(data['centers']);delta=np.asarray(data['delta']);changed=np.flatnonzero(abs(delta)>1.e-12)
        if not len(changed):raise ValueError('No QM correction proposed')
        if np.any(abs(delta[changed])>opt.step_A+1.e-9):
            raise ValueError('QM proposal exceeds the per-Apply current change limit; recalculate')
        for j in changed:b.validate_quadrupole_targets(names[j],[centers[j]+delta[j]])
        self.reader.reset_sampling()
        try:
            for j in changed:
                self.check();b.set_quadrupole_current(names[j],float(centers[j]+delta[j]))
            after,valid,tunes,evidence=self.signal(opt)
            mask=np.asarray(data['valid'],bool)
            if not valid[mask].all():raise ValueError('A fitted BPM became invalid after SET')
            dt=(tunes-np.asarray(data['initial_tunes'])+.5)%1-.5 if opt.measure_tunes else None
            orbit_change=after[2:,mask]-np.asarray(data['full_measured'],float)[2:,mask]
            data.update(full_after=after,after_valid=valid,after_tunes=tunes,after_tune_evidence=evidence,
                        tune_check_performed=opt.measure_tunes,
                        after_orbit_measurements=copy.deepcopy(self.reader.orbit_measurements))
            if opt.measure_tunes and np.max(abs(dt))>opt.tune_tolerance:
                raise ValueError('Measured tune change exceeds QM correction tolerance; restored')
            if opt.constrain_cod and np.max(abs(orbit_change))>opt.orbit_tolerance_mm:
                raise ValueError('Measured COD change exceeds QM correction tolerance; restored')
            _,weights,arc=qm_objective(data,opt,mask)
            targets=mask & (np.asarray(data['straight'],bool) | (arc & bool(opt.arc_weight)))
            before=np.asarray(data['full_measured'],float)
            if np.linalg.norm(after[0,targets]*weights[0,targets])>np.linalg.norm(before[0,targets]*weights[0,targets]):
                raise ValueError('Measured weighted horizontal dispersion did not improve; restored')
            self.check()
            expected=copy.deepcopy(data['baseline'])
            expected['quadrupoles'].update(zip(names,(centers+delta).tolist()))
            applied_baseline=self.snapshot(data['baseline']['quadrupoles'])
            if not self.settings_match(applied_baseline,expected,names):
                raise ValueError('Settings changed or QM targets were not reached after Apply; restored changed currents')
            data['applied_currents_A']=[applied_baseline['quadrupoles'][n] for n in names]
            data['applied_baseline']=applied_baseline
            data['applied_at']=datetime.now(timezone.utc).isoformat()
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
        if data['backend_id']!=self.backend.identity or not set(DispersionOptions.from_saved(data['options']).actuators())<=set(data['actuators']):
            raise ValueError('Machine or QM selection differs from the saved scan')
        origin=data.get('iteration_origin',data)
        skipped={r['magnet'] for r in data.get('probe_readbacks',[]) if r.get('skipped')}
        restore=[(n,c) for n,c in zip(data['actuators'],origin['centers']) if n not in skipped and n in DispersionOptions.from_saved(data['options']).actuators()]
        try:restore_quads(self.backend,[n for n,_ in restore],[c for _,c in restore])
        except Exception as exc:errors.append(str(exc))
        if data.get('checkpoint_schema') or data.get('matrix_only'):
            try:self.backend.restore_frequency(origin['baseline'].get('ramp_state',origin['baseline']['frequency']))
            except Exception as exc:errors.append(str(exc))
        if errors:
            data.update(restored=False,restoration_error='; '.join(errors))
            raise RuntimeError(data['restoration_error'])
        if 'iteration_origin' in data:
            data.setdefault('iteration_history',[]).append(dict(kind='restore_original',
                timestamp=datetime.now(timezone.utc).isoformat(),iteration=data.get('iteration_index',0)+1,
                was_applied=bool(data.get('applied'))))
            data.update(copy.deepcopy(origin))
            for key in ('iteration_origin','iteration_valid','delta','full_predicted','full_target',
                        'full_after','after_valid','applied_baseline','applied_currents_A'):
                data.pop(key,None)
            data['iteration_index']=0
        data['applied']=False;data['restored']=True
        data.pop('restoration_error',None)
