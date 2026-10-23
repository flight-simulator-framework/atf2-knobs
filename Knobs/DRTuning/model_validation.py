"""Offline model qualification and comparison; never creates an EPICS client.

python -m Knobs.DRTuning.model_validation --native --output /tmp/dr-model-check
python -m Knobs.DRTuning.model_validation --settings model.json --output out
Optional --reference consumes a versioned independent SAD/reference JSON.
"""
import argparse
import json
from pathlib import Path
import time
import numpy as np
from Knobs.DRCorrection.core import Options,CorrectionService,serial,save_session
from .model import OpticsModel,capture_settings
from .model_optics import transport,sr_beta_calibration,sr_dispersion_calibration,phase_calibration


def compare_optics(export,reference):
    if reference.get('schema')!='dr-optics-reference-1' or not reference.get('source'):
        raise ValueError('Expected dr-optics-reference-1 with independent source')
    table=export['optics'];rows=reference.get('rows',{})
    if not rows:raise ValueError('Reference has no optics rows')
    if set(rows)-set(table['rows']):raise ValueError('Reference locations missing from RFTrack export')
    if reference.get('settings_sha256'):
        from .model import digest
        if reference['settings_sha256']!=digest(export['settings']):raise ValueError('Reference input settings hash differs')
    errors=[]
    for n,r in rows.items():
        actual=np.asarray(table['rows'][n]['beta_m']);target=np.asarray(r['beta_m'],float)
        if target.shape!=(2,) or not np.isfinite(target).all() or np.any(target<=0):raise ValueError('Invalid reference beta: '+n)
        errors.append(actual/target-1)
        if abs(table['rows'][n]['s_m']-float(r['s_m']))>1.e-4:raise ValueError('Reference longitudinal position differs: '+n)
    tune=np.asarray(reference['tunes'],float)
    if tune.shape!=(2,) or not np.isfinite(tune).all():raise ValueError('Invalid reference tunes')
    error=(np.asarray(table['tunes'])-tune+.5)%1-.5
    return dict(source=reference['source'],locations=len(rows),tune_difference=error,
                beta_relative_rms=np.sqrt(np.mean(np.asarray(errors)**2,axis=0)),
                beta_relative_max=np.max(abs(np.asarray(errors)),axis=0),
                same_input_hash_verified=bool(reference.get('settings_sha256')),
                note='Comparison only; no claim of full SAD equivalence or live commissioning')


def compare_response(data,reference):
    """Name-aligned matrix/proposal comparison, units must match explicitly."""
    if reference.get('schema')!='dr-response-reference-1' or not reference.get('source'):
        raise ValueError('Expected dr-response-reference-1 with source')
    if reference['mode']!=data['mode'] or reference['actuator_unit']!=data['actuator_unit'] or reference.get('signal_units')!=('mm' if data['mode']!='dispersion' else 'dispersion:mm/delta;orbit:mm'):
        raise ValueError('Reference mode / units differ')
    if set(reference['names'])!=set(data['names']) or set(reference['actuators'])!=set(data['actuators']):raise ValueError('Reference BPM/actuator sets differ')
    for key in ('names','actuators'):
        if len(reference[key])!=len(set(reference[key])):raise ValueError('Duplicate reference '+key)
    n=len(data['names']);blocks=np.asarray(data['measured']).shape[0]
    indices=[reference['names'].index(n) for n in data['names']];columns=[reference['actuators'].index(n) for n in data['actuators']]
    matrix=np.asarray(reference['response'],float)
    if matrix.shape!=(n*blocks,len(columns)) or not np.isfinite(matrix).all():raise ValueError('Invalid reference response')
    matrix=matrix[[i+b*n for b in range(blocks) for i in indices]][:,columns]
    delta=np.asarray(data['response'])-matrix
    result=dict(source=reference['source'],matrix_relative_error=float(np.linalg.norm(delta)/max(np.linalg.norm(matrix),1.e-15)))
    if 'delta' in reference:
        proposal=np.asarray(reference['delta'],float)
        if proposal.shape!=(len(columns),) or not np.isfinite(proposal).all():raise ValueError('Invalid reference proposal')
        result['proposal_delta_difference']=np.asarray(data['delta'])-proposal[columns]
    return result


def native_validation(output):
    from .rftrack import RFTrackBackend
    from .sr_beta import fit_sr_beta
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_lattice import set_quadrupole_roll_error
    from Knobs.DRCorrection.dataio import fit_sr
    root=Path(output);root.mkdir(parents=True,exist_ok=True);start=time.monotonic()
    plant=RFTrackBackend();plant.noise_mm=0.
    # Hidden alignment error, deliberately absent from captured model inputs.
    plant.qelements['QM4R.1'].set_offsets(0.,5.e-6,0.)
    service=CorrectionService(plant);baseline=plant.snapshot();reports={}
    for mode in ('orbit','dispersion','skew'):
        if mode=='skew':
            # Unobserved skew field error, not an exported current setting.
            set_quadrupole_roll_error(plant.lattice,'QM4R.1',50.e-6)
        selected=plant.skews[:4] if mode=='skew' else plant.vcorrectors[:8]
        opt=Options(response_source='rftrack',count_v=8,step_A=.1,skew_step=.0003,
                    corrector_probe_A=.01,skew_probe=1.e-5,
                    probe1=plant.hcorrectors[0],probe2=plant.hcorrectors[len(plant.hcorrectors)//2],
                    fixed_actuators=tuple(n for n in plant.hcorrectors+plant.vcorrectors+plant.skews if n not in selected))
        original=plant.snapshot();t=time.monotonic();data=service.calculate(mode,opt)
        if not plant.same_settings(original,plant.snapshot()):raise AssertionError('CALC did not restore diagnostic settings')
        before=float(np.linalg.norm(data['measured']-data['target']))
        service.apply(data);after=float(np.linalg.norm(data['after']-data['target']))
        reports[mode]=dict(before_norm=before,after_norm=after,wall_s=time.monotonic()-t,
                           measured_settings=len(data['orbit_measurements']),response_source=data['source'])
        if after>=before:raise AssertionError(f'{mode}: no measured improvement: {before} -> {after}')
        save_session(root/(mode+'.json'),data);service.reset()
        if mode=='skew':set_quadrupole_roll_error(plant.lattice,'QM4R.1',0.)
    plant.qelements['QM4R.1'].set_offsets(0.,0.,0.)
    settings=capture_settings(plant,Options());model=OpticsModel(settings)
    # A shifted target exercises actual QF matching rather than a no-op fit.
    tunes=model.tunes();model.match_tunes(tunes+[.001,-.001])
    reports['tune_match']=model.match_report
    table=transport(model);beta=sr_beta_calibration(model,table);phase=phase_calibration(model,table)
    beta_results={}
    for i,axis in enumerate(('x','y')):
        names=list(settings['quadrupoles_k1l'])
        measured=[table['rows'][n]['average_beta_m'][i] for n in names]
        fit=fit_sr_beta(names,measured,beta['planes'][axis]);beta_results[axis]=fit
        for row in fit['monitors']:
            np.testing.assert_allclose(row['beta_m'],table['rows'][row['name']]['beta_m'][i],rtol=1.e-5)
    cal=sr_dispersion_calibration(model)
    measured=np.array([cal['planes'][a]['nominal_bpm'] for a in ('x','y')])
    data=dict(mode='dispersion',names=cal['names'],positions_m=[table['rows'][n.removeprefix('dr:')]['s_m'] for n in cal['names']],valid=[True]*3,full_measured=measured)
    sr=fit_sr(data,cal)
    reports['sr_beta']=beta_results;reports['sr_dispersion']=sr
    reports['phase_triplets']={a:len(phase['planes'][a]) for a in ('x','y')}
    # Extraction-constrained solve runs without touching the plant during CALC.
    opt=Options(response_source='rftrack',model_fix_extraction_orbit=True,count_v=8,
                fixed_actuators=tuple(plant.vcorrectors[8:]),step_A=.1)
    plant.write('correctors',[plant.vcorrectors[0]],[.02])
    constrained=service.calculate('orbit',opt);reports['extraction']=constrained['constraints'];save_session(root/'constrained.json',constrained)
    plant.restore(baseline)
    export=dict(schema='dr-model-export-1',settings=settings,tune_match=model.match_report,optics=table)
    (root/'model.json').write_text(json.dumps(serial(export),indent=2,allow_nan=False)+'\n')
    reports.update(wall_s=time.monotonic()-start,restored=plant.snapshot()==baseline,
                   sad_comparison='not run: supply independent reference with identical input settings',
                   qualification='noise-free simulation, one 5 um alignment / skew-error fixture; not live commissioning')
    (root/'report.json').write_text(json.dumps(serial(reports),indent=2,allow_nan=False)+'\n')
    return reports


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--native',action='store_true');parser.add_argument('--settings',type=Path)
    parser.add_argument('--reference',type=Path);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if args.native:
        report=native_validation(args.output);print(json.dumps(serial({k:v for k,v in report.items() if k in ('orbit','dispersion','skew','wall_s','restored')}),indent=2));return
    if not args.settings:parser.error('Supply --native or --settings')
    value=json.loads(args.settings.read_text());settings=value.get('settings',value);model=OpticsModel(settings)
    model.match_tunes(settings['tunes'],max_scale=settings.get('tune_match_max_scale',.05),alias=settings.get('tune_alias',False))
    export=dict(schema='dr-model-export-1',settings=settings,tune_match=model.match_report,optics=transport(model))
    if args.reference:export['comparison']=compare_optics(export,json.loads(args.reference.read_text()))
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'model.json').write_text(json.dumps(serial(export),indent=2,allow_nan=False)+'\n')


if __name__=='__main__':main()
