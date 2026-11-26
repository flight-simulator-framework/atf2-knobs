"""Measured response corrections, reversible acquisition and portable sessions.

The UI never invokes SAD shell scripts. CALC probes restore their settings;
SET is separate and refuses stale or foreign-backend proposals.
"""
from dataclasses import dataclass, asdict
import copy
import csv
import json
from pathlib import Path
from datetime import datetime,timezone
import threading
import numpy as np
from scipy.optimize import lsq_linear
from .assessment import changed_indices,assess
from .backends import MagnetReadbackError
from .probe_quality import mean_variance,response_quality,wider_probe


try:
    from machine_interfaces.Backend.Excitation import finite_abs_max,next_excitation_amplitude
    EXCITATION_RULE_SOURCE='machine_interfaces.Backend.Excitation'
except ModuleNotFoundError as exc:
    if exc.name!='machine_interfaces.Backend.Excitation':raise
    # Compatibility with installed machine-interfaces 0.2.0 packages which
    # predate Excitation.py. Keep the established SysID rule unchanged.
    EXCITATION_RULE_SOURCE='bundled SysID compatibility rule'
    def finite_abs_max(values):
        values=np.asarray(values,float).ravel();values=values[np.isfinite(values)]
        return float(np.max(abs(values))) if values.size else 0.
    def next_excitation_amplitude(current_amp,observed,target,max_range):
        if not np.isfinite(observed) or observed<=0:new_amp=current_amp*1.5
        else:new_amp=current_amp*max(.8,min(target/observed if target>0 else 1.,2.))
        if max_range>0:new_amp=min(new_amp,max_range)
        return .8*max(float(new_amp),1.e-6)+.2*current_amp


class Cancelled(RuntimeError):
    pass


class NoCommonBPMs(ValueError):
    """The acquisition has no valid BPM in the enabled selection."""


ZV_RETRY_STEP_A = .01


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
    adaptive_probe: bool = False
    adaptive_target_mm: float = .1
    adaptive_target_tolerance: float = .25
    adaptive_max_pairs: int = 8
    adaptive_max_probe: float = .5
    step_A: float = .1
    skew_step: float = .0007
    svd_cut: float = .04
    selection_method: str = 'greedy'
    regularization: float = .01
    dispersion_weight: float = .05
    orbit_weight: float = 1.
    excluded_bpms: tuple = ()
    fixed_actuators: tuple = ()
    preserve_vertical_dispersion_orbit: bool = False
    dispersion_target_snr: float = 5.
    orbit_target_snr: float | None = None


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


def validate_adaptive_probe(opt):
    if not opt.adaptive_probe:return
    if (not np.isfinite([opt.adaptive_target_mm,opt.adaptive_target_tolerance,opt.adaptive_max_pairs,opt.adaptive_max_probe]).all()
        or opt.adaptive_target_mm<=0 or not 0<opt.adaptive_target_tolerance<1
        or int(opt.adaptive_max_pairs)!=opt.adaptive_max_pairs or not 1<=opt.adaptive_max_pairs<=100
        or opt.adaptive_max_probe<=0):
        raise ValueError('Invalid adaptive probe target, tolerance, pair count or amplitude limit')


class CorrectionService:
    def __init__(self,backend):
        self.backend=backend
        self.cancel=threading.Event()
        self.pause_requested=threading.Event()
        self.paused=threading.Event()
        self.undo=[]
        self.orbit_measurements=[]
        self._model_manager=None

    @property
    def model_manager(self):
        if self._model_manager is None:
            from Knobs.DRTuning.model import ModelManager
            self._model_manager=ModelManager(self.backend)
        return self._model_manager

    dispersion_exclusions = None
    dispersion_observed = None

    def sync_dispersion_selection(self, mode, opt):
        if mode == 'dispersion' and self.dispersion_exclusions is not None:
            opt.excluded_bpms = self.dispersion_exclusions()

    def publish_dispersion_baseline(self, mode, valid):
        if mode == 'dispersion' and self.dispersion_observed is not None:
            self.dispersion_observed(dict(names=list(self.backend.bpm_names),
                positions_m=np.array(self.backend.positions_m), valid=valid.copy(),
                full_measured=self.full_signal.copy(),
                full_signal_labels=['Dispersion X','Dispersion Y','COD X','COD Y']))

    def check_cancel(self):
        if self.cancel.is_set():raise Cancelled('Cancelled; temporary scan settings restored')

    def pause_checkpoint(self,progress,percent,baseline=None,ignored=()):
        """Pause only outside probe transactions, with RF/magnets restored."""
        self.check_cancel()
        if not self.pause_requested.is_set():return
        def verify():
            if baseline is None:return
            current=self.backend.snapshot(excluded=ignored) if ignored else self.backend.snapshot()
            if not self.backend.same_settings(current,baseline):
                raise ValueError('Machine settings changed at pause/resume; CALC again. No correction applied.')
        verify()
        self.paused.set()
        try:
            progress(percent,'PAUSED: probe settings restored. Resume to continue, or Cancel to stop.')
            while self.pause_requested.is_set():
                if self.cancel.wait(.1):self.check_cancel()
            self.check_cancel()
            verify()
        finally:self.paused.clear()
        progress(percent,'RESUMED: continuing response measurement')

    def summarize_orbit(self,arrays):
        return dict(**{key:np.mean(arrays[key],axis=0) for key in ('x','y','tmit')},
                    valid=np.all(arrays['valid'],axis=0),
                    stdx=np.std(arrays['x'],axis=0),stdy=np.std(arrays['y'],axis=0))

    def _orbit(self,opt,on_sample=None,*,required_bpms=(),extra_updates=0,on_retry=None):
        collect=lambda:self._collect_orbit_samples(opt,on_sample,required_bpms,extra_updates,on_retry)
        stable_read=getattr(self.backend,'stable_beam_read',None)
        arrays,diagnostics,timestamps,fresh=stable_read(collect) if stable_read else collect()
        count=opt.bpm_samples
        interval=opt.bpm_interval_s
        summary=self.summarize_orbit(arrays)
        self.orbit_measurements.append(dict(names=list(self.backend.bpm_names),**summary,
            nshots=int(count),sample_interval_s=float(interval),samples=arrays,
            position_unit='mm',freshness_verified=all(fresh),timestamps=timestamps,
            same_shot_verified=False,validity_diagnostics=diagnostics))
        return summary['x'],summary['y'],summary['valid']

    def _collect_orbit_samples(self,opt,on_sample,required_bpms,extra_updates,on_retry):
        count=opt.bpm_samples;interval=opt.bpm_interval_s
        validate_bpm_sampling(opt)
        samples=[];diagnostics=[];timestamps=[];fresh=[]
        required=np.asarray(required_bpms,dtype=int)
        budget=int(count)+extra_updates
        stream=iter(self.backend.orbit_samples(budget,self.cancel,interval)) if hasattr(self.backend,'orbit_samples') else None
        rejected=0
        def replace_invalid(affected,widespread):
            nonlocal rejected
            rejected+=1
            names=', '.join(self.backend.bpm_names[i] for i in affected)
            if rejected>extra_updates:
                raise ValueError(f'Required BPM invalid after {extra_updates} retries: {names} '
                                 f'({len(samples)}/{int(count)} valid updates)')
            if on_retry is not None:on_retry(rejected,names,len(samples),widespread)
        for _ in range(budget):
            self.check_cancel()
            # Same sampling cadence as SysID, but wait BEFORE each sample
            # (including the first) instead of after it, to avoid an immediate
            # pre-change read. Total scheduled wait remains count * interval.
            if stream is None and interval and self.cancel.wait(interval):self.check_cancel()
            result=next(stream) if stream is not None else self.backend.orbit()
            self.check_cancel()
            x,y,valid=checked_orbit(result,self.backend.bpm_names)
            missing=required[~valid[required]]
            healthy=np.count_nonzero(valid)
            if extra_updates and required.size<5 and not missing.size and samples:
                # During preset discovery the outside BPM list is not known yet.
                # A later healthy frame can reveal that the very first shot was sparse.
                for i in reversed(range(len(samples))):
                    if healthy-np.count_nonzero(samples[i]['valid'])>=5:
                        earlier=samples.pop(i)
                        for field in (diagnostics,timestamps,fresh):field.pop(i)
                        replace_invalid(np.flatnonzero(valid & ~earlier['valid']),True)
                if samples and np.count_nonzero(samples[-1]['valid'])-healthy>=5:
                    replace_invalid(np.flatnonzero(samples[-1]['valid'] & ~valid),True)
                    continue
            widespread=bool(extra_updates and (2*healthy<len(valid)
                            or missing.size>=5 or (missing.size>=2 and 4*missing.size>=required.size)))
            if missing.size or widespread:
                affected=missing if missing.size else np.flatnonzero(~valid)
                replace_invalid(affected,widespread)
                continue
            samples.append(dict(x=x.copy(),y=y.copy(),tmit=np.asarray(result['tmit'],float).copy(),valid=valid.copy()))
            diagnostics.append(result.get('validity_diagnostics',{}))
            timestamps.append(result.get('timestamps',{}));fresh.append(result.get('freshness_verified',False))
            if on_sample is not None:on_sample(len(samples),int(count))
            if len(samples)==int(count):break
        arrays={key:np.stack([s[key] for s in samples]) for key in ('x','y','tmit','valid')}
        return arrays,diagnostics,timestamps,fresh

    def require_usable(self,names,measured,target,acquisition_valid,actuators,opt,measurements=None):
        enabled=np.array([n not in opt.excluded_bpms for n in names],bool)
        finite=np.isfinite(measured).all(axis=0)&np.isfinite(target).all(axis=0)
        valid=np.asarray(acquisition_valid,bool)&enabled&finite
        available=np.array([n not in opt.fixed_actuators for n in actuators],bool)
        if valid.any() and available.any():return valid,available
        message=(f'No valid enabled BPMs or actuators: plane={opt.plane}; '
            f'BPM total={len(names)}, enabled={int(enabled.sum())}, '
            f'acquisition-valid={int(np.sum(acquisition_valid))}, usable={int(valid.sum())}; '
            f'actuators enabled={int(available.sum())}/{len(actuators)}. ')
        if not enabled.any():message+='Select BPMs / magnets: enable BPMs for the fit. '
        elif not valid.any():
            message+='No common valid BPMs across the acquired samples/settings. '
        if not available.any():message+='Enable magnets for the selected plane/family. '
        frames=self.orbit_measurements if measurements is None else measurements
        if (opt.response_source=='rftrack_nominal' and len(frames)==3 and
                all('names' in frame and 'valid' in frame.get('samples',{}) for frame in frames)):
            fit_names=np.asarray(names)[enabled]
            for label,frame in zip(('RF baseline','RF minus','RF plus'),frames):
                rows=np.isin(frame['names'],fit_names)
                shots=np.asarray(frame['samples']['valid'],bool)[:,rows]
                per_shot=shots.sum(axis=1)
                message+=(f'{label}: {int(shots.all(axis=0).sum())} BPMs valid on every update; '
                          f'{int(per_shot.min())}–{int(per_shot.max())} per update. ')
        elif frames:
            counts=[int(np.sum(frame['valid'])) for frame in frames]
            last=frames[-1];samples=last.get('samples',last)
            x=np.asarray(samples['x']);y=np.asarray(samples['y']);q=np.asarray(samples['tmit'])
            message+=(f'Setting valid counts: min={min(counts)}, max={max(counts)} over {len(frames)} settings. '
                f'Last setting: nonfinite x/y entries={int(np.sum(~np.isfinite(x)|~np.isfinite(y)))}, '
                f'nonpositive/nonfinite intensity entries={int(np.sum(~np.isfinite(q)|(q<=0)))}. ')
            diagnostics=last.get('validity_diagnostics',[])
            if diagnostics:message+='BPM source/status per sample: '+str(diagnostics)+'. '
        message+='Check COD mode, beam/intensity and BPM selection; no correction applied.'
        if enabled.any() and available.any() and not (enabled & acquisition_valid).any():
            raise NoCommonBPMs(message)
        raise ValueError(message)

    def _checked_orbit(self,opt,expected,mode):
        orbit=self._orbit(opt)
        self.sync_dispersion_selection(mode,opt)
        enabled=~np.isin(self.backend.bpm_names,opt.excluded_bpms)
        if (enabled.any() and not (enabled & orbit[2]).any()) or (expected is not None and np.any(expected & enabled & ~orbit[2])):
            if self.cancel.wait(3.):self.check_cancel()
            orbit=self._orbit(opt)
            # Keep the three accepted RF points contiguous for downstream fits.
            # The rejected raw batch remains available inside the replacement.
            rejected=self.orbit_measurements.pop(-2)
            self.orbit_measurements[-1]['retry_of']=rejected
        return orbit

    def _signal(self,mode,opt,*,retry_dispersion=True):
        n=len(self.backend.bpm_names)
        def orbit_point():
            if opt.orbit_target_snr is None:return self._orbit(opt)
            expected=self.orbit_measurements[-1]['valid'] if self.orbit_measurements else None
            return self._checked_orbit(opt,expected,mode)
        if mode=='orbit':
            x,y,mask=orbit_point()
            self.full_signal=np.array([x,y])
            return np.array([x if opt.plane=='x' else y]),mask
        if mode=='dispersion':
            origin=self.backend.frequency()
            frequency_state=self.backend.frequency_state()
            span=opt.frequency_change_khz*1000
            if not np.isfinite(span) or span<=0 or not np.isfinite(opt.momentum_compaction) or opt.momentum_compaction<=0:
                raise ValueError('Positive frequency span and momentum compaction required')
            self.backend.validate_frequency_targets([origin-span/2,origin+span/2])
            def read_point(expected):
                return self._checked_orbit(opt,expected,mode) if retry_dispersion else self._orbit(opt)
            expected=np.ones(n,bool)
            if self.orbit_measurements:
                expected=np.asarray(self.orbit_measurements[-1]['valid'],bool)
                if not expected.any():expected=np.ones(n,bool)
            baseline=read_point(expected)
            common=baseline[2].copy()
            try:
                frames=[]
                for sign in (-1,1):
                    self.backend.set_frequency(origin+sign*span/2)
                    point=read_point(common)
                    frames.append(point)
                    common &= point[2]
            finally:self.backend.restore_frequency(frequency_state)
            index=0 if opt.plane=='x' else 1
            delta=-span/(self.backend.nominal_frequency_hz*opt.momentum_compaction)
            self.full_signal=np.array([(frames[1][0]-frames[0][0])/delta,(frames[1][1]-frames[0][1])/delta,baseline[0],baseline[1]])
            return np.array([(frames[1][index]-frames[0][index])/delta,baseline[index]]),baseline[2]&frames[0][2]&frames[1][2]
        if mode=='skew':
            if opt.probe1==opt.probe2:raise ValueError('Choose two different horizontal steering probes')
            signals=[];mask=np.ones(n,bool)
            for name,span in ((opt.probe1,opt.change1_A),(opt.probe2,opt.change2_A)):
                if name not in self.backend.hcorrectors or not np.isfinite(span) or span<=0:
                    raise ValueError('Choose two valid horizontal probes with positive full current spans')
                origin=self.backend.read('correctors',[name])[0]
                try:
                    frames=[]
                    for sign in (-1,1):
                        self.backend.write('correctors',[name],[origin+sign*span/2])
                        frames.append(orbit_point())
                finally:self.backend.write('correctors',[name],[origin])
                # Native op-base plots show the vertical orbit difference for
                # each horizontal excitation, not a hidden coupling coefficient.
                signals.append(frames[1][1]-frames[0][1]);mask &= frames[0][2]&frames[1][2]
            self.full_signal=np.array(signals)
            return np.array(signals),mask
        raise ValueError('Unknown correction mode')

    def _measure_probe_signal(self,mode,opt,previous_valid,names):
        # The serial probe path already implements its own recovery and adaptive shrink.
        value,mask=self._signal(mode,opt,retry_dispersion=False)
        self.sync_dispersion_selection(mode,opt)
        valid=previous_valid & mask
        self.require_usable(self.backend.bpm_names,value,np.zeros_like(value),valid,names,opt)
        return value,valid

    def acquire(self,mode,opt,progress=lambda *_:None,*,retry_zv_probes=False):
        validate_bpm_sampling(opt)
        validate_adaptive_probe(opt)
        if opt.response_source=='rftrack_nominal':
            from Knobs.DRTuning.nominal_zv import acquire
            return acquire(self,mode,opt,progress)
        if opt.response_source=='rftrack':
            try:return self.model_manager.acquire(self,mode,opt,progress)
            except Exception:
                self.model_manager.model=None;self.model_manager.optics=None
                raise
        if opt.response_source!='measured':raise ValueError('Unknown response source')
        snr_scan=mode in ('orbit','skew') and opt.adaptive_probe and opt.orbit_target_snr is not None
        if snr_scan and (opt.bpm_samples<2 or not np.isfinite(opt.orbit_target_snr) or opt.orbit_target_snr<=0):
            raise ValueError('Adaptive response requires at least two BPM samples and positive minimum S/N')
        self.orbit_measurements=[]
        self.backend.validate_mode(mode)
        if opt.plane not in ('x','y'):raise ValueError('Select x or y')
        # Validate the nominal target before moving any magnet or RF setting.
        nominal=self.backend.target_dispersion(opt.plane) if mode=='dispersion' else None
        if mode=='dispersion':
            shape=np.shape(nominal);expected=(len(self.backend.bpm_names),)
            if nominal is None or shape!=expected or not np.isfinite(nominal).all():
                reason='missing target' if nominal is None else 'BPM count/array shape mismatch'
                if nominal is not None and shape==expected:
                    bad=np.asarray(self.backend.bpm_names)[~np.isfinite(nominal)]
                    reason='nonfinite target at '+', '.join(bad)
                raise ValueError(f'Invalid nominal dispersion target: plane={opt.plane}; expected shape={expected}, '
                    f'received shape={shape}; {reason}. Load target dispersion with one finite value per BPM (mm per dp/p). '
                    'No magnet or RF setting changed.')
        if mode=='skew':
            names=[n for n in self.backend.skews if (opt.sd and n.startswith('SD')) or (opt.sf and n.startswith('SF'))]
            kind='skews';probe=opt.skew_probe
        else:
            names=list(self.backend.hcorrectors if opt.plane=='x' else self.backend.vcorrectors)
            kind='correctors';probe=opt.corrector_probe_A
        names=[n for n in names if n not in opt.fixed_actuators]
        if not names:raise ValueError('No enabled correctors')
        if not np.isfinite(probe) or probe<=0:raise ValueError('Positive probe required')
        scan_errors=[];self.last_scan_errors=scan_errors
        progress(0,'Reading initial magnet currents; unreadable magnets will be skipped')
        baseline=(self.backend.measured_snapshot(scan_errors) if hasattr(self.backend,'measured_snapshot')
                  else self.backend.snapshot())
        ignored=[e['magnet'] for e in scan_errors]
        for error in scan_errors:progress(0,f"SKIP {error['magnet']}: {error['error']}")
        names=[n for n in names if n not in ignored]
        if not names:
            raise RuntimeError('No usable response magnets.\n'+'\n'.join(e['error'] for e in scan_errors))
        centers=np.asarray([baseline[kind][n] for n in names] if hasattr(self.backend,'measured_snapshot') else self.backend.read(kind,names),float)
        low,high=self.backend.limits(kind,names)
        self.pause_checkpoint(progress,0,baseline,ignored)
        measured,valid=self._signal(mode,opt)
        self.publish_dispersion_baseline(mode,valid)
        self.sync_dispersion_selection(mode,opt)
        self.require_usable(self.backend.bpm_names,measured,np.zeros_like(measured),valid,names,opt)
        full_measured=self.full_signal.copy();columns=[];full_columns=[];probe_history=[]
        self.last_probe_history=probe_history
        successful=[];set_blocked_reason=''
        enabled=np.array([n not in opt.excluded_bpms for n in self.backend.bpm_names])
        # Each probe owns its restoration, including failures/cancellation.
        # _signal likewise restores RF and skew-excitation correctors. Do not
        # replay the full snapshot: it includes magnets this scan never touched.
        for j,name in enumerate(names):
            self.pause_checkpoint(progress,int(100*j/len(names)),baseline,ignored)
            valid_before=valid.copy();restored=False;restoration_error=''
            try:
                amplitude=min(probe,opt.adaptive_max_probe) if opt.adaptive_probe else probe
                pairs=int(opt.adaptive_max_pairs) if opt.adaptive_probe else (Options.adaptive_max_pairs if retry_zv_probes else 1)
                initial_pair=True
                skip_reason=''
                for attempt in range(pairs):
                    self.pause_checkpoint(progress,int(100*j/len(names)),baseline,ignored)
                    self.check_cancel();values=[];full_values=[];readbacks=[];restored=False
                    lo=max(low[j],centers[j]-amplitude);hi=min(high[j],centers[j]+amplitude)
                    if hi<=lo:raise ValueError('No available probe range')
                    pair_valid=valid.copy()
                    reduced=initial_pair and attempt>0
                    try:
                        for setting in (lo,hi):
                            delay=float(getattr(self.backend,'profile',{}).get('magnet_settle_s',0.))
                            progress(int(100*j/len(names)),f'{name}: pair {attempt+1}/{pairs}, set {setting:.6g} {self.backend.unit(kind)}; current readback + {delay:g} s extra wait; BPM {opt.bpm_samples} samples, {opt.bpm_interval_s:g} s interval (first read delayed)')
                            self.backend.write(kind,[name],[setting])
                            try:
                                value,accepted=self._measure_probe_signal(mode,opt,pair_valid,names)
                            except NoCommonBPMs:
                                if not retry_zv_probes or attempt==0:raise
                                delay_s=3.
                                probe_history.append(dict(magnet=name,pair=attempt+1,amplitude=amplitude,
                                    setting_A=setting,status='retry_bpm',delay_s=delay_s,used_for_response=False))
                                progress(int(100*j/len(names)),f'{name}: no common BPM at {setting:g} A; waiting 3 s before remeasuring this endpoint once')
                                if self.cancel.wait(delay_s):self.check_cancel()
                                try:value,accepted=self._measure_probe_signal(mode,opt,pair_valid,names)
                                except NoCommonBPMs as repeated:
                                    raise NoCommonBPMs(f'{name}: no common BPM after retry at {setting:g} A. {repeated}') from repeated
                            pair_valid=accepted
                            values.append(value);full_values.append(self.full_signal.copy())
                            if opt.adaptive_probe or reduced:readbacks.append(float(self.backend.read(kind,[name])[0]))
                    except BaseException as exc:
                        try:
                            if hasattr(self.backend,'restore_probe'):self.backend.restore_probe(kind,name,centers[j])
                            else:self.backend.write(kind,[name],[centers[j]])
                            restored=True
                        except Exception as restoration:
                            restoration_error=f'Probe restoration also failed for {name}: {restoration}'
                            if not isinstance(exc,MagnetReadbackError) or set(exc.names)!={name}:
                                raise RuntimeError(f'{exc}\n{restoration_error}') from exc
                        if not (restored and retry_zv_probes and initial_pair and isinstance(exc,NoCommonBPMs)):
                            raise
                        self.check_cancel()
                        progress(int(100*j/len(names)),f'{name}: no common BPM at {setting:g} A; restored to {centers[j]:g} A. Checking beam at original current.')
                        reference,reference_valid=self._signal(mode,opt)
                        self.sync_dispersion_selection(mode,opt)
                        try:
                            self.require_usable(self.backend.bpm_names,reference,np.zeros_like(reference),
                                valid & reference_valid,names,opt,self.orbit_measurements[-3:])
                        except ValueError as recovery:
                            raise ValueError(f'{name}: BPM coverage unavailable at original current {centers[j]:g} A. {recovery}') from recovery
                        # Shrink the actual failed excursion, including a supply-clipped endpoint.
                        excursion=min(amplitude,abs(setting-centers[j]))
                        half_steps=np.floor(excursion/(2*ZV_RETRY_STEP_A)+1.e-10)
                        smaller=ZV_RETRY_STEP_A*max(1,half_steps)
                        retry=smaller<excursion-1.e-10 and attempt+1<pairs
                        probe_history.append(dict(magnet=name,pair=attempt+1,amplitude=amplitude,unit='A',
                            commanded=[lo,hi],failed_setting_A=setting,readbacks=readbacks,restored=True,
                            used_for_response=False,status='no_common_bpm',next_width_A=smaller if retry else None))
                        if not retry:
                            skip_reason=f'No common BPM at {setting:g} A; minimum probe width or pair limit reached'
                            break
                        progress(int(100*j/len(names)),f'{name}: retry initial probe ±{amplitude:g} → ±{smaller:g} A')
                        amplitude=smaller
                        continue
                    else:
                        self.backend.write(kind,[name],[centers[j]])
                        restored=True
                    valid=pair_valid
                    initial_pair=False
                    span=hi-lo
                    if not opt.adaptive_probe and not reduced:break
                    # Use physical readback differences, not assumed movement
                    # within the supply's allowed convergence tolerance.
                    span=readbacks[1]-readbacks[0]
                    difference=(values[1]-values[0])/2.
                    # Dispersion acquisition contains eta and nominal COD. Target
                    # a position response in mm, never eta in mm/(dp/p).
                    signal=difference[1] if mode=='dispersion' else difference
                    if mode=='dispersion':
                        self.sync_dispersion_selection(mode,opt)
                        enabled=np.array([n not in opt.excluded_bpms for n in self.backend.bpm_names])
                    quality=None
                    if snr_scan:
                        use=pair_valid & enabled
                        if mode=='orbit':
                            noise2=sum(mean_variance(f,opt.plane) for f in self.orbit_measurements[-2:])
                        else:
                            # Each of the two diagnostic steerers contributes its own +/- Y pair at each skew endpoint.
                            variances=np.array([mean_variance(f,'y') for f in self.orbit_measurements[-8:]])
                            noise2=variances.reshape(2,2,2,-1).sum(axis=(0,2))
                        quality=response_quality(np.sqrt(np.mean((values[1]-values[0])[...,use]**2)),
                                                 np.sqrt(np.mean(noise2[...,use])),opt.orbit_target_snr)
                        observed=quality['signal_rms']
                        target_change=quality['noise_rms']*opt.orbit_target_snr
                        reached=quality['usable']
                    else:
                        observed=finite_abs_max(signal[...,valid & enabled])
                        target_change=opt.adaptive_target_mm
                        relative_change=observed/target_change
                        if mode=='dispersion' and opt.plane=='y':
                            reached=relative_change>=1-opt.adaptive_target_tolerance
                        else:
                            reached=abs(relative_change-1)<=opt.adaptive_target_tolerance
                    record=dict(magnet=name,pair=attempt+1,amplitude=amplitude,unit=self.backend.unit(kind),
                        commanded=[lo,hi],readbacks=readbacks,readback_span=span,observed_mm=observed,
                        target_mm=target_change,target_reached=bool(reached),used_for_response=False,
                        valid_bpms=int(np.sum(valid & enabled)),status='retry')
                    if quality:record['quality']=quality
                    probe_history.append(record)
                    if not np.isfinite(readbacks).all() or span<=1.e-12:
                        record['status']='invalid_current_span'
                        raise MagnetReadbackError([name],f'{name}: adaptive probe has no positive readback span: '
                            f'commanded {lo:g}/{hi:g}, readbacks {readbacks} {self.backend.unit(kind)}; '
                            'original current restored; no correction calculated')
                    if reached or reduced:
                        record['status']='target_reached' if reached else 'reduced_initial_probe'
                        record['used_for_response']=True
                        progress(int(100*(j+(attempt+1)/pairs)/len(names)),
                            f'{name}: accepted pair {attempt+1} at ±{amplitude:g} {record["unit"]}; BPM response {observed:.5g} mm')
                        break
                    next_amplitude=wider_probe(amplitude,quality,opt.adaptive_max_probe) if quality else min(opt.adaptive_max_probe,
                        next_excitation_amplitude(amplitude,observed,opt.adaptive_target_mm,opt.adaptive_max_probe))
                    next_lo=max(low[j],centers[j]-next_amplitude);next_hi=min(high[j],centers[j]+next_amplitude)
                    unchanged=np.allclose([next_lo,next_hi],[lo,hi],rtol=1.e-9,atol=1.e-12)
                    if unchanged:record['status']='amplitude_or_supply_limit'
                    elif attempt+1==pairs:record['status']='max_pairs'
                    progress(int(100*(j+(attempt+1)/pairs)/len(names)),
                        f'{name}: pair {attempt+1}/{pairs}; BPM response {observed:.5g} / target {record["target_mm"]:g} mm; '
                        f'{record["status"]}; next probe ±{next_amplitude:.5g} {self.backend.unit(kind)}')
                    if record['status']!='retry':
                        if quality:
                            skip_reason=f'Below S/N {opt.orbit_target_snr:g} at ±{amplitude:g} {self.backend.unit(kind)}'
                            valid=valid_before
                        else:record['used_for_response']=True
                        break
                    amplitude=next_amplitude
            except MagnetReadbackError as exc:
                # A failed skew diagnostic excitation is a global measurement
                # failure, not evidence that the outer skew supply is faulty.
                if set(exc.names)!={name}:raise
                if not restored and not restoration_error:
                    try:
                        if hasattr(self.backend,'restore_probe'):self.backend.restore_probe(kind,name,centers[j])
                        else:self.backend.write(kind,[name],[centers[j]])
                        restored=True
                    except Exception as restoration:
                        restoration_error=f'Probe restoration also failed for {name}: {restoration}'
                error=dict(magnet=name,stage='response probe',error=str(exc),restored=restored,
                    restoration='original current restored and verified' if restored else restoration_error)
                scan_errors.append(error)
                if not restored:set_blocked_reason='Probe restoration unverified; resolve skipped magnet errors and CALC again before SET.'
                for record in probe_history:
                    if record['magnet']==name:record['used_for_response']=False
                valid=valid_before
                progress(int(100*(j+1)/len(names)),f"SKIP {name}: {error['error']}; {error['restoration']}")
                continue
            if skip_reason:
                scan_errors.append(dict(magnet=name,stage='response probe' if snr_scan else 'initial ZV probe',error=skip_reason,
                    restored=True,restoration='original current restored; BPM coverage verified'))
                progress(int(100*(j+1)/len(names)),f'SKIP {name}: {skip_reason}')
                continue
            successful.append(j)
            columns.append(((values[1]-values[0])/span).reshape(-1))
            full_columns.append(((full_values[1]-full_values[0])/span).reshape(-1))
            progress(int(100*(j+1)/len(names)),f'{name}: response {j+1}/{len(names)}')
        self.pause_checkpoint(progress,100,baseline,ignored)
        if not successful:
            raise RuntimeError('No usable response magnets.\n'+'\n'.join(
                f"{e['magnet']}: {e['error']}; {e['restoration']}" for e in scan_errors))
        low=np.broadcast_to(low,centers.shape)[successful];high=np.broadcast_to(high,centers.shape)[successful]
        names=[names[j] for j in successful];centers=centers[successful]
        target=np.zeros_like(measured)
        if mode=='dispersion':target[0]=nominal
        return dict(schema='dr-correction-1',mode=mode,options=asdict(opt),
            backend_id=self.backend.identity,names=list(self.backend.bpm_names),
            positions_m=list(self.backend.positions_m),actuators=names,kind=kind,
            actuator_unit=self.backend.unit(kind),baseline=baseline,centers=centers,
            measured=measured,target=target,response=np.column_stack(columns),valid=valid,
            orbit_measurements=self.orbit_measurements,
            probe_history=probe_history,scan_errors=scan_errors,ignored_readbacks=ignored,
            set_blocked_reason=set_blocked_reason,
            probe_rule_source='Measured response S/N' if snr_scan else EXCITATION_RULE_SOURCE if opt.adaptive_probe else 'fixed amplitude',
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
        self.sync_dispersion_selection(mode,opt)
        if data.get('schema')!='dr-correction-1' or data['mode']!=mode:
            raise ValueError('File is not a matching correction dataset')
        if data['options']['plane']!=opt.plane and mode!='skew':
            raise ValueError('Select the same H/V plane as the file before CALC (FILE)')
        # Keep acquisition metadata independent of the current fit controls.
        # In particular, excluding BPMs from one fit must not erase valid raw
        # measurements when the user includes those BPMs in a subsequent fit.
        data.setdefault('acquisition_options',copy.deepcopy(data['options']))
        data.setdefault('acquisition_valid',copy.deepcopy(data['valid']))
        data['valid']=copy.deepcopy(data['acquisition_valid'])
        for key in ('after','full_after','after_valid','after_orbit_measurements','after_set_settle_s','assessment','applied_actuators'):data.pop(key,None)
        names=data['names'];actuators=data['actuators']
        measured=np.asarray(data['measured'],float);target=np.asarray(data['target'],float)
        response=np.asarray(data['response'],float)
        if measured.shape!=target.shape or measured.ndim!=2 or measured.shape[1]!=len(names):
            raise ValueError('Signal shape mismatch')
        if mode=='dispersion' and opt.plane=='y' and opt.preserve_vertical_dispersion_orbit:
            # Both model screening and measured fits use the beam orbit at nominal RF.
            target=np.stack((target[0],measured[1]))
        # Saved JSON represents missing BPM samples as null. Give every solver
        # the same numeric arrays, with NaN at those positions.
        data.update(measured=measured,target=target,response=response)
        if response.shape!=(measured.size,len(actuators)):
            raise ValueError('Invalid response matrix')
        selected=copy.deepcopy(opt)
        if mode=='skew':selected.fixed_actuators=tuple(set(opt.fixed_actuators)|{
            n for n in actuators if not ((opt.sd and n.startswith('SD')) or (opt.sf and n.startswith('SF')))})
        valid,available=self.require_usable(names,measured,target,data['valid'],actuators,selected,data.get('orbit_measurements',[]))
        signal_regions=None
        if mode=='dispersion' and opt.plane=='y' and opt.preserve_vertical_dispersion_orbit:
            from .dispersion_bpms import vertical_arc_bpms,cod_change_bpms
            signal_regions=np.stack((vertical_arc_bpms(names),cod_change_bpms(names)))
            if opt.dispersion_weight and not (valid & signal_regions[0]).any():
                raise ValueError('No valid enabled vertical arc BPMs for dispersion correction')
            data['fit_bpm_regions']=signal_regions
            mask=(signal_regions & valid).ravel()
            if not (mask & np.repeat([opt.dispersion_weight>0,opt.orbit_weight>0],len(names))).any():
                raise ValueError('No valid enabled BPMs for the weighted dispersion or COD-change region')
        else:
            data.pop('fit_bpm_regions',None)
            mask=np.tile(valid,measured.shape[0])
        if not np.isfinite(response[mask]).all():
            raise ValueError('Invalid response matrix')
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
            matrix=s[keep,None]*vt[keep];rhs=-(u[:,keep].T@residual)
            if opt.selection_method=='all' and opt.regularization:
                matrix=np.vstack((matrix,opt.regularization*s[0]*np.eye(len(indices))))
                rhs=np.r_[rhs,np.zeros(len(indices))]
            fit=lsq_linear(matrix,rhs,bounds=(lower[indices],upper[indices]))
            if not fit.success:raise RuntimeError('Bounded solve failed')
            return fit.x,s,int(keep.sum())
        symmetric=np.all(lower[available]==-step_limit) and np.all(upper[available]==step_limit)
        response_rms=np.sqrt(np.mean(a[:,available]**2,axis=0))
        diagnostic=', '.join(f'{name}: {value:.6g}' for name,value in zip(np.asarray(actuators)[available],response_rms))
        diagnostic=f'plane={opt.plane}; valid BPMs={int(valid.sum())}; weighted response RMS per {data["actuator_unit"]}: [{diagnostic}]'
        if np.linalg.norm(residual)>1e-14 and not np.any(a[:,available]):
            raise ValueError('No measured response: all enabled response columns are zero. '+diagnostic+
                '. Check magnet readbacks, BPM updates and settling; no correction was applied.')
        if opt.selection_method not in ('greedy','all') or not np.isfinite(opt.regularization) or opt.regularization<0:
            raise ValueError('Invalid selection method / regularization')
        if opt.selection_method=='all' and np.linalg.norm(residual)>1e-14:
            selected=list(np.flatnonzero(available));step,singular_values,rank=solve(selected)
            delta=np.zeros(len(actuators));delta[selected]=step
            solver='All enabled magnets: bounded regularized truncated SVD'
        elif symmetric and constraints is None and np.linalg.norm(residual)>1e-14:
            from .ported_solver import propose,ClosedOrbitError
            try:delta,selected_names,singular_values,rank=propose(data,opt,valid,available,maximum,step_limit,signal_regions)
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
            full_response=np.asarray(data['full_response'],float)
            data['full_predicted']=full+(full_response@delta).reshape(full.shape)
            data['full_fitted']=full-data['full_predicted']
        return data

    def measure_selected_zvs(self,proposal,progress=lambda *_:None,*,group_rf=False):
        """Probe only the ZVs selected by a vertical RFTrack dispersion fit."""
        opt=Options(**proposal['options'])
        if (proposal['mode']!='dispersion' or opt.plane!='y' or opt.response_source not in ('rftrack','rftrack_nominal')
                or opt.selection_method!='greedy' or proposal.get('applied')
                or proposal['backend_id']!=self.backend.identity):
            raise ValueError('Run vertical dispersion CALC with RFTrack and Greedy first')
        names=list(proposal['selected'])
        allowed=set(self.backend.vcorrectors).intersection(proposal['actuators']).difference(opt.fixed_actuators)
        if not names or len(set(names))!=len(names) or len(names)>opt.count_v or not set(names)<=allowed:
            raise ValueError('No valid RFTrack ZV shortlist. Run CALC again.')
        self.check_cancel()
        if not self.backend.same_settings(self.backend.snapshot(),proposal['baseline']):
            raise ValueError('Settings changed since RFTrack selection. Run CALC again.')
        if opt.response_source=='rftrack':
            from Knobs.DRTuning.model import verify_inputs
            verify_inputs(self.backend,proposal['model_settings'])
        opt.response_source='measured'
        scan=copy.deepcopy(opt)
        scan.fixed_actuators=tuple(sorted(set(opt.fixed_actuators) | (set(self.backend.vcorrectors)-set(names))))
        if group_rf:
            from Knobs.DRTuning.zv_rf_scan import acquire_zvs
            data=acquire_zvs(self,scan,progress)
        else:data=self.acquire('dispersion',scan,progress,retry_zv_probes=True)
        data['rftrack_candidates']=names
        if 'reference_matrix' in proposal:
            data['reference_matrix']=copy.deepcopy(proposal['reference_matrix'])
        else:
            data['selection_model_sha256']=proposal.get('model_settings_sha256')
        if proposal.get('constraints') is not None:
            constraints=copy.deepcopy(proposal['constraints'])
            columns=[proposal['actuators'].index(n) for n in data['actuators']]
            constraints['matrix']=np.asarray(constraints['matrix'])[:,columns]
            data['constraints']=constraints
            data['model_settings']=copy.deepcopy(proposal['model_settings'])
        self.check_cancel()
        return self.calculate('dispersion',opt,progress,dataset=data)

    def response_reuse_issue(self,data,opt):
        if data.get('backend_id')!=self.backend.identity:return 'Response belongs to another backend.'
        original=Options(**data.get('acquisition_options',data['options']))
        if original.plane!=opt.plane:return 'No response saved for this plane.'
        if original.response_source!=opt.response_source:return 'Response source changed.'
        keys=['momentum_compaction'] if data['mode']=='dispersion' else []
        if data['mode']=='skew':keys+=['probe1','probe2','change1_A','change2_A']
        if opt.response_source=='rftrack':
            keys+=['energy_gev','tune_x','tune_y','model_match_tunes','model_regional_targets',
                   'model_fix_extraction_orbit','model_fix_extraction_dispersion','model_extraction_weight']
        changed=[key for key in keys if getattr(original,key)!=getattr(opt,key)]
        if changed:return 'Response definition changed: '+', '.join(changed)+'. Measure again.'
        missing_bpms=set(self.backend.bpm_names)-set(opt.excluded_bpms)-set(data['names'])
        if missing_bpms:return 'No saved response rows for BPMs: '+', '.join(sorted(missing_bpms))+'. Measure again.'
        if data['mode']=='skew':
            names=[n for n in self.backend.skews if (opt.sd and n.startswith('SD')) or (opt.sf and n.startswith('SF'))]
        else:names=self.backend.hcorrectors if opt.plane=='x' else self.backend.vcorrectors
        for scope in ('rftrack_candidates','full_zv_candidates'):
            if scope in data:names=set(names).intersection(data[scope])
        missing=set(names)-set(opt.fixed_actuators)-set(data['actuators'])-{e['magnet'] for e in data.get('scan_errors',[])}
        if missing:return 'No measured columns for: '+', '.join(sorted(missing))+'. Measure again.'
        return ''

    def observe_current_beam(self,mode,opt,ignored=(),*,before=None):
        b=self.backend
        b.validate_mode(mode);validate_bpm_sampling(opt);self.check_cancel()
        def snapshot():return b.snapshot(excluded=ignored) if ignored else b.snapshot()
        if before is None:before=snapshot()
        self.orbit_measurements=[]
        measured,valid=self._signal(mode,opt)
        actual=snapshot()
        if not b.same_settings(before,actual):raise ValueError(f'Settings changed during {mode} measurement')
        if not (valid & ~np.isin(b.bpm_names,opt.excluded_bpms)).any():raise NoCommonBPMs('No valid enabled BPM in beam measurement')
        self.check_cancel()
        return dict(baseline=actual,measured=measured,valid=valid,full_measured=self.full_signal.copy(),
                    frames=self.orbit_measurements,frequency_change_khz=opt.frequency_change_khz,
                    momentum_compaction=opt.momentum_compaction,measured_at=datetime.now(timezone.utc).isoformat(),
                    names=list(b.bpm_names),positions_m=b.positions_m,
                    full_signal_labels=['Dispersion X','Dispersion Y','COD X','COD Y'] if mode=='dispersion' else [])

    def reuse_on_current_beam(self,data,opt,progress=lambda *_:None,*,observation=None):
        """Combine a measured matrix with the current observation, supply centers and limits."""
        mode=data['mode']
        if mode not in ('orbit','skew','dispersion') or opt.response_source!='measured':
            raise ValueError('Select a measured response for this correction')
        if mode=='dispersion' and (opt.plane!='y' or not (data.get('rftrack_candidates') or data.get('full_zv_candidates'))):
            raise ValueError('Load a measured vertical ZV response')
        issue=self.response_reuse_issue(data,opt)
        if issue:raise ValueError(issue)
        b=self.backend
        kind='skews' if mode=='skew' else 'correctors'
        magnets=b.skews if mode=='skew' else b.vcorrectors if opt.plane=='y' else b.hcorrectors
        if (data.get('kind')!=kind or list(data['names'])!=list(b.bpm_names)
                or not set(data['actuators'])<=set(magnets) or data.get('actuator_unit')!=b.unit(kind)):
            raise ValueError('BPM or corrector mapping / units differ from the saved response')
        if data.get('set_blocked_reason'):raise ValueError(data['set_blocked_reason'])
        if data.get('model_settings'):
            from Knobs.DRTuning.model import verify_inputs
            verify_inputs(b,data['model_settings'])
        b.validate_mode(mode)
        validate_bpm_sampling(opt)
        ignored=data.get('ignored_readbacks')
        def snapshot():return b.snapshot(excluded=ignored) if ignored else b.snapshot()
        actual=snapshot()
        if (observation is None or observation['frequency_change_khz']!=opt.frequency_change_khz
                or observation['momentum_compaction']!=opt.momentum_compaction or not b.same_settings(actual,observation['baseline'])):
            progress(0,f'Measuring current beam for {mode}')
            observation=self.observe_current_beam(mode,opt,ignored,before=actual)
            actual=observation['baseline']
        measured,valid=observation['measured'],observation['valid']
        saved=dict(data)
        if 'baseline' in data:saved.setdefault('response_baseline',data['baseline'])
        saved.setdefault('response_valid',data.get('acquisition_valid',data['valid']))
        saved.update(applied=False,baseline=actual,centers=[actual[kind][n] for n in data['actuators']],
                     limits=b.limits(kind,data['actuators']),full_measured=observation['full_measured'],measured=measured,
                     baseline_orbit_measurements=observation['frames'],baseline_measured_at=observation['measured_at'],baseline_valid=valid,
                     acquisition_valid=np.asarray(saved['response_valid'],bool)&valid)
        saved.pop('applied_baseline',None)
        saved.pop('bpm_observations',None)
        self.check_cancel()
        return self.recalculate(saved,opt,progress)

    def recalculate(self,data,opt,progress=lambda *_:None):
        """Refit saved measurements only; no orbit reads or machine writes."""
        issue=self.response_reuse_issue(data,opt)
        if issue:raise ValueError(issue)
        saved=copy.deepcopy(data)
        if saved.get('applied') and saved.get('applied_baseline') and (saved.get('full_zv_candidates') or saved.get('rftrack_candidates')):
            # Reuse the measured slopes about the latest verified operating point.
            state=saved['applied_baseline']
            saved.setdefault('response_baseline',saved['baseline'])
            saved['baseline']=state
            saved['centers']=[state['correctors'][n] for n in saved['actuators']]
            saved['full_measured']=np.asarray(saved['full_after'],float)
            saved['measured']=np.asarray(saved['after'],float)
            saved.setdefault('response_valid',saved['acquisition_valid'])
            saved['acquisition_valid']=np.asarray(saved['acquisition_valid'],bool)&np.asarray(saved['after_valid'],bool)
            saved.pop('applied_baseline')
        if saved['mode']=='dispersion' and opt.response_source=='measured':
            nominal=np.asarray(self.backend.target_dispersion(opt.plane),float)
            if nominal.shape!=(len(self.backend.bpm_names),) or not np.isfinite(nominal).all():
                raise ValueError('Invalid nominal dispersion target for saved-response recalculation')
            saved['target'][0]=nominal[[self.backend.bpm_names.index(n) for n in saved['names']]]
        progress(0,'Recalculating saved response; no new beam measurements or magnet scans')
        result=self.calculate(saved['mode'],opt,progress,dataset=saved)
        result['reused_response']=True
        return result

    def apply(self,data,progress=lambda *_:None):
        self.check_cancel()
        validate_bpm_sampling(Options(**data['options']))
        if data.get('backend_id')!=self.backend.identity:raise ValueError('Proposal belongs to another backend')
        if data['options'].get('response_source')=='rftrack_nominal':
            raise ValueError('Measure the selected ZVs before Apply')
        if data.get('applied'):raise ValueError('Already applied; CALC again before another SET')
        changed=changed_indices(data)
        if not len(changed):raise ValueError('No magnet settings change; CALC already proposes zero correction')
        if data.get('set_blocked_reason'):raise ValueError(data['set_blocked_reason'])
        before=(self.backend.snapshot(excluded=data['ignored_readbacks']) if data.get('ignored_readbacks')
                else self.backend.snapshot())
        if not self.backend.same_settings(before,data['baseline']):raise ValueError('Settings changed since CALC; recalculate')
        if data.get('model_settings'):
            from Knobs.DRTuning.model import verify_inputs
            verify_inputs(self.backend,data['model_settings'])
        self.backend.validate_mode(data['mode'])
        if data.get('scan_errors') or data.get('full_zv_candidates') or data.get('rftrack_candidates'):
            # Rollback and Undo restore only supplies involved in this correction.
            touched={data['actuators'][i] for i in changed}
            if data['mode']=='skew':touched.update((data['options']['probe1'],data['options']['probe2']))
            before=copy.deepcopy(before)
            if data.get('full_zv_candidates') or data.get('rftrack_candidates'):
                before.pop('quadrupoles',None)
            for kind in ('correctors','skews','quadrupoles'):
                if kind in before:before[kind]={n:v for n,v in before[kind].items() if n in touched}
        self.orbit_measurements=[]
        try:
            values=np.asarray(data['centers'])+np.asarray(data['delta'])
            self.backend.write(data['kind'],[data['actuators'][i] for i in changed],values[changed])
            # Wait once after ALL correction currents have settled, before
            # starting any verification orbit/RF/skew measurement. Keep CALC
            # probe timing and the existing BPM averaging cadence unchanged.
            progress(0,'SET complete: waiting 5 s before after-SET measurement')
            self.cancel.wait(5.)
            self.check_cancel()
            progress(0,'Measuring after SET')
            measured,valid=self._signal(data['mode'],Options(**data['options']))
            if data.get('full_zv_candidates') or data.get('rftrack_candidates'):
                expected=copy.deepcopy(data['baseline'])
                expected['correctors'].update((data['actuators'][i],values[i]) for i in changed)
                actual=(self.backend.snapshot(excluded=data['ignored_readbacks']) if data.get('ignored_readbacks')
                        else self.backend.snapshot())
                if not self.backend.same_settings(actual,expected):
                    raise ValueError('Settings changed during vertical dispersion Apply')
                data['applied_baseline']=actual
        except Exception:
            self.backend.restore(before);raise
        self.undo.append(before)
        indices=[self.backend.bpm_names.index(n) for n in data['names']]
        data['after']=measured[:,indices];data['after_valid']=valid[indices];data['applied']=True
        data['full_after']=self.full_signal[:,indices].copy()
        data['after_orbit_measurements']=self.orbit_measurements
        data['after_set_settle_s']=5.
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
