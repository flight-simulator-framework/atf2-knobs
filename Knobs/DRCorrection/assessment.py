"""Pure numerical summaries of a proposal and its measured outcome."""
import numpy as np


def changed_indices(data):
    """Exactly the settings that SET will write; no display-only threshold."""
    before=np.asarray(data['centers'],float);delta=np.asarray(data['delta'],float)
    if before.shape!=(len(data['actuators']),) or delta.shape!=before.shape or not np.isfinite([before,delta]).all():
        raise ValueError('Invalid proposal settings')
    return np.flatnonzero(before+delta!=before)


def assess(data):
    """Compare on the same enabled, finite BPMs before and after SET.

    Ratios are descriptive, not statistical significance or calibrated alarms.
    Missing/all-invalid after data must never look like a successful correction.
    """
    before=np.asarray(data['measured'],float);predicted=np.asarray(data['predicted'],float)
    target=np.asarray(data['target'],float);after=np.asarray(data['after'],float)
    if before.shape!=predicted.shape or before.shape!=target.shape or before.shape!=after.shape:
        raise ValueError('Assessment signal shape mismatch')
    enabled=np.asarray(data['valid'],bool);after_valid=np.asarray(data['after_valid'],bool)
    common=enabled&after_valid
    rows=[]
    for i,label in enumerate(data['signal_labels']):
        mask=common&np.isfinite([before[i],predicted[i],target[i],after[i]]).all(axis=0)
        count=int(mask.sum());unit='mm / (dp/p)' if data['mode']=='dispersion' and i==0 else 'mm'
        row=dict(signal=label,unit=unit,valid_bpms=count,enabled_bpms=int(enabled.sum()),
                 excluded_from_comparison=int(enabled.sum())-count,
                 status='unavailable',before_rms=None,predicted_rms=None,after_rms=None,
                 improvement_pct=None,mismatch_rms=None,mismatch_max=None,worst_bpm=None,
                 mismatch_to_predicted_change=None,bpm_mismatch=[None]*len(enabled))
        if count:
            rms=lambda v:float(np.sqrt(np.mean(v*v)))
            b=rms((before[i]-target[i])[mask]);p=rms((predicted[i]-target[i])[mask]);a=rms((after[i]-target[i])[mask])
            diff=(after[i]-predicted[i])[mask];m=rms(diff)
            change=rms((predicted[i]-before[i])[mask])
            row.update(before_rms=b,predicted_rms=p,after_rms=a,
                       improvement_pct=100*(1-a/b) if b>1.e-12 else None,
                       mismatch_rms=m,mismatch_max=float(np.max(abs(diff))),
                       worst_bpm=data['names'][np.flatnonzero(mask)[np.argmax(abs(diff))]],
                       mismatch_to_predicted_change=m/change if change>1.e-12 else None,
                       status='improved' if a<b else 'worsened' if a>b else 'unchanged')
            row['bpm_mismatch']=[float(after[i,j]-predicted[i,j]) if mask[j] else None for j in range(len(mask))]
        rows.append(row)
    return dict(schema='dr-correction-assessment-1',rows=rows,
                note='RMS relative to target on common valid BPMs; positive improvement is better. '
                     'Prediction mismatch includes noise, drift and response errors; no significance threshold is assumed.')
