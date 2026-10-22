"""Observable, bounded DR correction runner for simulation only.

Uses the same CorrectionService as DRTuning. Events are suitable for a future
operator display; this module neither starts a GUI nor enables live control.
The fixed policy judges measured correction proxies, not beam emittance.
"""
import argparse
import copy
from dataclasses import asdict
import json
from pathlib import Path
import threading
import time

import numpy as np

from .backends import DemoBackend, StoredBackend
from .core import Cancelled, CorrectionService, Options, save_session, serial
from Knobs.DRTuning.rftrack import RFTrackBackend


class EventJournal:
    """Append and flush events in execution order; subscribers receive copies."""
    def __init__(self, path, subscriber=None):
        self.stream=Path(path).open('x',encoding='utf-8')
        self.subscriber=subscriber
        self.sequence=0
        self.started=time.monotonic()

    def emit(self, event, stage, message, **details):
        self.sequence+=1
        row=serial(dict(schema='dr-agent-event-1',sequence=self.sequence,
                        elapsed_wall_s=time.monotonic()-self.started,
                        event=event,stage=stage,message=message,**details))
        self.stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
        self.stream.flush()
        if self.subscriber:self.subscriber(copy.deepcopy(row))
        return row

    def close(self):
        self.stream.close()


def assess(data, minimum_improvement=.1):
    """Require common BPM coverage, measured improvement and charge retention.

    A proxy threshold is not a statistical significance test or an emittance
    measurement. Each measured row is returned separately for operator review.
    """
    mask=np.asarray(data['valid'],bool)
    after_mask=np.asarray(data['after_valid'],bool)
    if not mask.any() or not np.all(after_mask[mask]):
        return dict(accepted=False,reason='Required BPM coverage was lost')
    before=np.asarray(data['measured'],float)[:,mask]
    after=np.asarray(data['after'],float)[:,mask]
    target=np.asarray(data['target'],float)[:,mask]
    if not np.isfinite(before).all() or not np.isfinite(after).all():
        return dict(accepted=False,reason='Nonfinite measurement')
    weights=np.ones((len(before),1))
    if data['mode']=='dispersion':
        weights[:,0]=[data['options']['dispersion_weight'],data['options']['orbit_weight']]
    rms=lambda value:float(np.sqrt(np.mean(value**2)))
    old=rms((before-target)*weights);new=rms((after-target)*weights)
    # Compare corresponding baseline RF/excitation frames, not response probes.
    frames=len(data['after_orbit_measurements'])
    previous=data['orbit_measurements'][:frames]
    current=data['after_orbit_measurements']
    if not frames or len(previous)!=frames:
        return dict(accepted=False,reason='Missing validation frames')
    charge_ratios=[]
    for a,b in zip(previous,current):
        a=np.asarray(a['tmit'],float)[mask];b=np.asarray(b['tmit'],float)[mask]
        if not np.isfinite(a).all() or not np.isfinite(b).all() or np.any(a<=0):
            return dict(accepted=False,reason='Invalid BPM intensity')
        charge_ratios.extend(b/a)
    charge_ok=min(charge_ratios)>=.98
    improved=old>0 and new<=(1-minimum_improvement)*old
    # For joint dispersion/COD correction, do not trade a worse COD for a
    # smaller weighted sum. This pilot does not implement multi-stage retries.
    orbit_ok=data['mode']!='dispersion' or rms(after[1]-target[1])<=rms(before[1]-target[1])+1e-12
    accepted=bool(charge_ok and improved and orbit_ok)
    return dict(accepted=accepted,reason=(
        'Measured proxy improved; BPM intensity and coverage retained' if accepted else
        'BPM intensity decreased' if not charge_ok else
        'COD worsened during dispersion correction' if not orbit_ok else
        'Measured improvement below threshold'),
        weighted_rms_before=old,weighted_rms_after=new,
        signal_rms_before=np.sqrt(np.mean((before-target)**2,axis=1)),
        signal_rms_after=np.sqrt(np.mean((after-target)**2,axis=1)),
        minimum_charge_ratio=float(min(charge_ratios)),
        minimum_improvement=minimum_improvement,emittance_verified=False)


class SimulationCorrectionAgent:
    """One sequential controller; policy and optimizers have separate roles."""
    def __init__(self, backend, directory, subscriber=None):
        # EpicsBackend derives from DemoBackend: isinstance would enable live
        # control accidentally. This prototype deliberately accepts exact types.
        if type(backend) not in (DemoBackend,StoredBackend,RFTrackBackend):
            raise ValueError('This runner supports only DEMO and Stored DR simulation or native RFTrack')
        self.backend=backend
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=False)
        self.journal=EventJournal(self.directory/'events.jsonl',subscriber)
        self.service=CorrectionService(backend)
        self.stop_requested=threading.Event()
        self.running=False
        self.finished=False

    def request_stop(self):
        self.stop_requested.set()
        self.service.cancel.set()

    def check_stop(self):
        if self.stop_requested.is_set():raise Cancelled('Stop requested')

    def stage(self, mode, options):
        self.check_stop()
        before=self.backend.snapshot()
        applied=False
        data=None
        self.journal.emit('stage_started',mode,'Measure response, propose a correction, then validate it',
                          options=asdict(options),baseline_settings=before)
        try:
            def progress(percent,message):
                self.check_stop()
                self.journal.emit('progress',mode,message,percent=percent)
                self.check_stop()
            data=self.service.calculate(mode,options,progress)
            if not self.backend.same_settings(before,self.backend.snapshot()):
                raise RuntimeError('CALC did not restore the initial settings')
            self.journal.emit('proposal',mode,'Measured response produced a bounded correction',
                              magnets=data['actuators'],unit=data['actuator_unit'],
                              initial=data['centers'],delta=data['delta'],rank=data['rank'],
                              predicted=data['predicted'],measured=data['measured'],
                              reason='Reduce the measured residual using the existing correction solver')
            self.check_stop()
            self.journal.emit('applying',mode,'Apply the proposal and measure the resulting response')
            self.check_stop()
            self.service.apply(data);applied=True
            self.check_stop()
            decision=assess(data)
            self.journal.emit('assessment',mode,decision['reason'],**decision)
            self.check_stop()
            if not decision['accepted']:
                self.journal.emit('restoring',mode,'Reject the proposal and restore the pre-stage settings')
                self.service.reset();applied=False
                if not self.backend.same_settings(before,self.backend.snapshot()):
                    raise RuntimeError('Rejected proposal settings were not restored')
            data['agent_decision']=decision
            save_session(self.directory/(mode+'.json'),data)
            self.journal.emit('stage_completed',mode,
                              'Correction retained' if decision['accepted'] else 'Original settings restored',
                              accepted=decision['accepted'],settings=self.backend.snapshot())
            return dict(mode=mode,**serial(decision))
        except Exception as error:
            restoration_error=None
            if applied:
                try:self.service.reset()
                except Exception as failure:restoration_error=str(failure)
            try:restored=self.backend.same_settings(before,self.backend.snapshot())
            except Exception as failure:restored=False;restoration_error=str(failure)
            # A report failure must never prevent the restoration above.
            try:self.journal.emit('stage_stopped',mode,str(error),restored=restored,
                                  restoration_error=restoration_error)
            except Exception:pass
            if not restored or restoration_error:
                raise RuntimeError(f'{error}; restoration unverified: {restoration_error}') from error
            raise

    def run(self, plan):
        if self.running or self.finished:raise RuntimeError('Use a new run directory for each execution')
        self.running=True
        summary=dict(schema='dr-agent-run-1',backend=self.backend.label,status='running',stages=[],
                     policy='fixed measured-response policy',learning=False,emittance_verified=False)
        if type(self.backend) is RFTrackBackend:
            summary['model_scope']=dict(orbit='native RFTrack periodic transverse orbit',
                frequency='momentum offset using fixed momentum compaction; RF disabled',
                intensity='constant simulated signal; no loss validation',
                bpm_noise_rms_mm=self.backend.noise_mm,radiation_equilibrium=False)
        try:
            self.journal.emit('run_started','run','Start sequential simulation correction',backend=self.backend.label)
            for mode,options in plan:
                summary['stages'].append(self.stage(mode,options))
            self.check_stop()
            summary['status']='completed'
        except Exception as error:
            summary['status']='cancelled' if isinstance(error,Cancelled) else 'failed'
            summary['error']=str(error)
        finally:
            self.running=False;self.finished=True
            summary['elapsed_wall_s']=time.monotonic()-self.journal.started
            if type(self.backend) is StoredBackend:
                summary['measurement_counts']=dict(self.backend.interface.counts)
                summary['equivalent_measurement_seconds']=self.backend.interface.time_s
            else:
                summary['measurement_counts']={'orbit':self.backend.acquisitions}
                summary['equivalent_measurement_seconds']=5.*self.backend.acquisitions
            (self.directory/'summary.json').write_text(json.dumps(serial(summary),ensure_ascii=False,indent=2)+'\n')
            try:self.journal.emit('run_completed','run',summary['status'],summary=summary)
            finally:self.journal.close()
        return summary


def default_plan(backend):
    """A small public-catalog subset; catalog order need not start with SD."""
    skews=[name for name in backend.skews if name.startswith('SD')]
    if not skews or len(backend.hcorrectors)<2 or len(backend.vcorrectors)<2:
        raise ValueError('Pilot requires two H/V correctors and an SD skew')
    keep=backend.vcorrectors[:2]+skews[:1]
    options=Options(count_v=2,frequency_change_khz=2.,
                    probe1=backend.hcorrectors[0],probe2=backend.hcorrectors[len(backend.hcorrectors)//2],
                    fixed_actuators=tuple(n for n in backend.hcorrectors+backend.vcorrectors+backend.skews if n not in keep))
    return [(mode,options) for mode in ('orbit','dispersion','skew')]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=('demo','stored','rftrack'),default='demo')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seed',type=int,default=32)
    parser.add_argument('--native-pilot-errors',action='store_true',
                        help='RFTrack only: start with two vertical corrector offsets and one SD skew')
    args=parser.parse_args()
    if args.output.exists():parser.error('Use a new output directory')
    if args.native_pilot_errors and args.backend!='rftrack':
        parser.error('--native-pilot-errors requires --backend rftrack')
    if args.backend=='rftrack':backend=RFTrackBackend(seed=args.seed)
    elif args.backend=='stored':backend=StoredBackend(args.output/'plant',seed=args.seed)
    else:backend=DemoBackend()
    # StoredDRInterface creates its plant directory at construction, so keep
    # the event/results directory distinct from that plant directory.
    def report(row):
        message=row['message']
        if row['event']=='assessment' and 'weighted_rms_before' in row:
            message+=f'; weighted RMS {row["weighted_rms_before"]:.6g} -> {row["weighted_rms_after"]:.6g}; minimum intensity ratio {row["minimum_charge_ratio"]:.4f}'
        print(f'{row["sequence"]:04d} [{row["stage"]}] {message}',flush=True)
    try:
        if args.native_pilot_errors:
            # Scenario preparation is separate from policy: these values are
            # never supplied as correction targets or as an inverse solution.
            backend.write('correctors',backend.vcorrectors[:2],[.08,-.05])
            backend.write('skews',[next(n for n in backend.skews if n.startswith('SD'))],[.0005])
        agent=SimulationCorrectionAgent(backend,args.output/'agent',report)
        if args.native_pilot_errors:
            agent.journal.emit('scenario','run','Pilot with two steering offsets and one skew offset',
                               seed=args.seed,initial_readbacks=backend.snapshot(),
                               alignment_errors=False,random_strength_errors=False)
        summary=agent.run(default_plan(backend))
        return 0 if summary['status']=='completed' else 1
    finally:backend.close()


if __name__=='__main__':raise SystemExit(main())
