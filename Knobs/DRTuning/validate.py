"""Native RFTrack integration and independent offline physics checks.

Only this validator inspects nominal optics. The measurement analysis does not.
Run: python -m Knobs.DRTuning.validate --output /tmp/drtuning-validation
"""
import argparse
import json
from pathlib import Path
import threading
import time
import numpy as np
from Knobs.DRCorrection.core import serial
from Knobs.DRCorrection.tbt import analyze_tbt
from .optics import QModService,phase_beta
from .rftrack import RFTrackBackend


def validate(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True);start=time.monotonic()
    b=RFTrackBackend();baseline=b.snapshot();name='QF2R.1';origin=b.quadrupole_current(name)
    scan=QModService(b,threading.Event()).scan(name,origin+np.linspace(-.02,.02,5))
    if b.snapshot()!=baseline:raise AssertionError('Settings were not restored')
    model=json.loads((Path(__file__).parent/'forms/nominal_phase_optics.json').read_text());phases={}
    for axis,names in [('x',b.hcorrectors),('y',b.vcorrectors)]:
        wave=b.tbt(256,dict(corrector=names[0],delta_A=.1,turn=1))
        estimate=analyze_tbt(wave,start_turn=3);phases[axis]=phase_beta(estimate,model)['planes'][axis]
        (output/f'tbt_{axis}.json').write_text(json.dumps(serial(dict(waveform=wave,estimate=estimate)),indent=2)+'\n')
    report=dict(wall_s=time.monotonic()-start,scan=scan,phase=phases,restored=b.snapshot()==baseline,
                tbt_acquisitions=b.acquisitions,measurement_time_s=5*b.acquisitions,
                time_basis='5 s per TBT batch; excludes magnet settling/operator time')
    (output/'measurements.json').write_text(json.dumps(serial(report),indent=2)+'\n')
    reference=check_reference(report)
    (output/'reference_check.json').write_text(json.dumps(serial(reference),indent=2)+'\n')
    if max(abs(np.array(reference['qmod_relative_error'])))>.05:raise AssertionError('Q modulation disagrees with reference by >5%')
    if any(v['rms_relative']>.05 or v['n']<80 for v in reference['phase_vs_public_tfs'].values()):raise AssertionError('Phase beta failed nominal qualification')
    return report,reference


def check_reference(report):
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_lattice import build_atf_dr_lattice
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_twiss import _linear_map,_periodic_twiss,load_atf_dr_twiss
    ring=build_atf_dr_lattice();m=_linear_map(ring,np.zeros(4),1e-5)
    q=ring['QF2R.1'];q=q[0] if isinstance(q,list) else q;k=q.get_K1(-1299.9999);length=q.get_length()
    if abs(q.get_S('exit')-length)>1e-8:raise ValueError('Reference formula assumes QF2R.1 starts at the ring origin')
    expected=[]
    for sl,kplane in [(slice(0,2),k),(slice(2,4),-k)]:
        beta,alpha,_=_periodic_twiss(m[sl,sl]);s=np.linspace(0,length,1001);root=np.sqrt(abs(kplane))
        c=np.cos(root*s) if kplane>0 else np.cosh(root*s)
        ss=(np.sin(root*s) if kplane>0 else np.sinh(root*s))/root
        expected.append(float(np.trapezoid(beta*c*c-2*alpha*c*ss+(1+alpha*alpha)/beta*ss*ss,s)/length))
    lookup={r['NAME']:r for r in load_atf_dr_twiss()};errors={}
    for axis,field in [('x','BETX'),('y','BETY')]:
        ratios=np.array([v['beta_m']/lookup[v['names'][0][3:]][field]-1 for v in report['phase'][axis] if v['valid']])
        errors[axis]=dict(median_relative=float(np.median(ratios)),rms_relative=float(np.sqrt(np.mean(ratios**2))),n=len(ratios))
    return dict(qmod_reference_average_beta_m=expected,
        qmod_relative_error=(np.array(report['scan']['fit']['beta_m'])/expected-1).tolist(),phase_vs_public_tfs=errors)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    report,reference=validate(args.output)
    print(json.dumps(serial(dict(wall_s=report['wall_s'],restored=report['restored'],reference=reference)),indent=2))

if __name__=='__main__':main()
