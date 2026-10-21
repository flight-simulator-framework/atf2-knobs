"""Measurement-only optics analysis. No simulator imports or lattice access."""
import numpy as np


def fit_qmod(k1l, tunes, used=None):
    """Port of sad_beta/prog_beta_calc: beta = abs(4 pi dQ/dK1L).

    The sign of each slope is also retained (real TBT aliases may reverse it).
    Returns magnet-averaged beta, not beta transported to a nearby BPM.
    """
    k=np.asarray(k1l,float);q=np.asarray(tunes,float)
    if k.ndim!=1 or q.shape!=(len(k),2):raise ValueError('Expected K1L[n], tunes[n,2]')
    use=np.ones(len(k),bool) if used is None else np.asarray(used,bool)
    if use.shape!=k.shape:raise ValueError('Invalid point selection')
    if np.count_nonzero(use)<3 or not np.isfinite(k[use]).all() or not np.isfinite(q[use]).all():
        raise ValueError('At least three finite selected tune measurements required')
    x=k[use];y=q[use];x=x-x.mean()
    if np.ptp(x)<1e-12:raise ValueError('No quadrupole strength span')
    # Unwrap integer tune boundaries, but do not invent a branch through 0 or .5.
    y=np.unwrap(y*2*np.pi,axis=0)/(2*np.pi)
    design=np.column_stack([np.ones(len(x)),x]);coef=np.linalg.lstsq(design,y,rcond=None)[0]
    residual=y-design@coef
    slope_error=np.sqrt(np.sum(residual**2,axis=0)/(len(x)-2)/np.sum(x*x))
    return dict(beta_m=abs(4*np.pi*coef[1]),beta_error_m=4*np.pi*slope_error,
        slope=coef[1],intercept_at_mean=coef[0],mean_k1l=float(k[use].mean()),
        residual_tune=residual,used=use,method='op-base linear tune vs K1L fit',
        location='average inside the modulated quadrupole',sign_convention='absolute slope, as legacy op-base')


def phase_beta(estimate,model):
    """Three-BPM method: beta_i=(cot(phi_ij)-cot(phi_ik))/(M11ij/M12ij-M11ik/M12ik).

    model is an independently supplied, versioned nominal transfer calibration.
    Each plane has triplets: {names:[i,j,k], m11_m12:[a,b], phase_sign:+1|-1}.
    No amplitude calibration is required. Exclude near-singular phase advances.
    """
    if model.get('schema')!='dr-phase-optics-1' or not model.get('source'):
        raise ValueError('A versioned public phase-optics calibration is required')
    names=estimate['names'];out={}
    for axis in ('x','y'):
        result=estimate['planes'][axis];rows=[]
        if not result.get('valid'):out[axis]=rows;continue
        phase=np.asarray(result['phase_rad']);valid=np.asarray(result['valid_bpms'],bool)
        for triplet in model.get('planes',{}).get(axis,[]):
            row=dict(names=triplet['names'],valid=False,beta_m=None)
            try:
                indices=[names.index(n) for n in triplet['names']]
                a,b=np.asarray(triplet['m11_m12'],float);sign=triplet['phase_sign']
                if len(indices)!=3 or len(set(indices))!=3 or sign not in (-1,1):raise ValueError('Invalid triplet')
                if not valid[indices].all():raise ValueError('Unmeasurable BPM')
                advances=sign*(phase[indices[1:]]-phase[indices[0]])
                if not np.isfinite([a,b,*advances]).all() or abs(a-b)<1e-8 or np.min(abs(np.sin(advances)))<.05:
                    raise ValueError('Ill-conditioned phase advance or transport')
                beta=float((1/np.tan(advances[0])-1/np.tan(advances[1]))/(a-b))
                if not np.isfinite(beta) or beta<=0:raise ValueError('Nonphysical beta; check branch/model/coupling')
                row.update(valid=True,beta_m=beta)
            except (ValueError,KeyError) as error:row['reason']=str(error)
            rows.append(row)
        out[axis]=rows
    return dict(method='three-BPM phase beta',source=model['source'],planes=out,
        limitations='uncoupled linear optics approximation; nominal transfer errors propagate')


class QModService:
    def __init__(self,backend,cancel):self.backend=backend;self.cancel=cancel
    def scan(self,name,values,progress=lambda *_:None):
        values=np.asarray(values,float)
        if values.ndim!=1 or len(values)<3:raise ValueError('At least three scan points required')
        self.backend.validate_quadrupole_targets(name,values)
        if hasattr(self.backend,'validate_tune_measurement'):self.backend.validate_tune_measurement()
        origin=self.backend.quadrupole_current(name)
        self.backend.validate_quadrupole_targets(name,[origin])
        rows=[]
        try:
            for i,value in enumerate(values):
                if self.cancel.is_set():raise RuntimeError('Q modulation cancelled')
                self.backend.set_quadrupole_current(name,float(value))
                reading=self.backend.measure_tunes(cancel=self.cancel)
                actual=self.backend.quadrupole_current(name)
                rows.append(dict(current_A=actual,k1l=self.backend.quadrupole_k1l(name,actual),
                                 tunes=reading['tunes'],measurement=reading))
                if hasattr(self.backend,'quadrupole_field_check'):
                    check=self.backend.quadrupole_field_check(name,actual);rows[-1]['field_check']=check
                    if check is not None and not check['within_tolerance']:raise RuntimeError('Magnet calibration disagrees with the configured field PV')
                progress(round(100*(i+1)/len(values)),f'{name}: {i+1}/{len(values)}')
        finally:
            self.backend.set_quadrupole_current(name,origin)
        result=fit_qmod([r['k1l'] for r in rows],[r['tunes'] for r in rows])
        return dict(schema='dr-qmod-1',source=self.backend.label,name=name,origin_A=origin,
                    restored=True,rows=rows,fit=result)
