"""Observable XSR size + local beta estimates, with explicit dispersion handling.

Uncoupled projected/effective geometric emittance, not a normal-mode emittance
measurement. XSR SIGMA PVs are source-plane RMS micrometres, resolution-corrected
(operator confirmation 2026-10-23). Never subtract that resolution a second time.
"""
import math
import time


XSR_SIZE_PVS={'y':'XSR:PROJ_V_SIGMA'}


def estimate_emittance(sigma_um,beta_m,dispersion_mm=0.,sigma_delta=0.,*,
                       correct_dispersion=False,resolution_um=0.,uncertainties=None):
    """Independent first-order uncertainty propagation; values use source plane.

    uncertainties uses the same keys and units as the five scalar inputs. None
    means unknown uncertainty, not a zero-error estimate. sigma_delta = RMS dp/p.
    """
    values=dict(sigma_um=float(sigma_um),beta_m=float(beta_m),dispersion_mm=float(dispersion_mm),
                sigma_delta=float(sigma_delta),resolution_um=float(resolution_um))
    if not all(math.isfinite(v) for v in values.values()):raise ValueError('Finite size / optics inputs required')
    s,b,d,q,r=(values[k] for k in ('sigma_um','beta_m','dispersion_mm','sigma_delta','resolution_um'))
    if s<=0 or b<=0 or q<0 or r<0:raise ValueError('Positive RMS size and beta; nonnegative spread and resolution required')
    if correct_dispersion and d!=0 and q==0:raise ValueError('Nonzero dispersion requires a positive RMS dp/p')
    # All terms below are in um^2, giving pm rad directly after division by beta.
    dispersive=(1000*d*q)**2 if correct_dispersion else 0.
    variance=s*s-r*r-dispersive;epsilon=variance/b
    uncertainty=None
    if uncertainties is not None:
        if set(uncertainties)!=set(values):raise ValueError('Supply independent 1-sigma errors for all five inputs')
        u={k:float(v) for k,v in uncertainties.items()}
        if not all(math.isfinite(v) and v>=0 for v in u.values()):raise ValueError('Invalid input uncertainty')
        gradients=dict(sigma_um=2*s/b,beta_m=-variance/b**2,resolution_um=-2*r/b,
                       dispersion_mm=-2.e6*d*q*q/b if correct_dispersion else 0.,
                       sigma_delta=-2.e6*d*d*q/b if correct_dispersion else 0.)
        uncertainty=math.sqrt(sum((gradients[k]*u[k])**2 for k in values))
    else:u=None
    status='resolved' if variance>0 else 'unresolved: nonpositive betatron variance'
    if variance>0 and uncertainty is not None and epsilon<=uncertainty:status='consistent with zero within supplied 1-sigma errors'
    return dict(schema='dr-xsr-emittance-plane-1',inputs=values,input_uncertainties=u,
                dispersion_corrected=bool(correct_dispersion),
                estimate_kind='dispersion-corrected effective geometric' if correct_dispersion else 'apparent (dispersion not removed)',
                apparent_pm_rad=s*s/b,emittance_pm_rad=epsilon if variance>0 else None,
                emittance_m_rad=epsilon*1.e-12 if variance>0 else None,
                uncertainty_pm_rad=uncertainty,betatron_variance_um2=variance,
                dispersive_variance_um2=dispersive,resolution_variance_um2=r*r,status=status,
                assumptions='Uncoupled sigma^2 = beta*epsilon + (eta*sigma_delta)^2 + resolution^2; '
                            'same source point and optics; no x-beta/delta correlation or normal-mode separation.')


def read_xsr_sizes(get):
    """Read the corrected vertical size only. No acquisition trigger or writes."""
    raw={axis:float(get(pv)) for axis,pv in XSR_SIZE_PVS.items()}
    if not all(math.isfinite(v) and v>0 for v in raw.values()):raise ValueError('XSR RMS size PVs must be finite and positive')
    return dict(schema='dr-xsr-size-1',sigma_um=raw,pvs=XSR_SIZE_PVS.copy(),read_unix_s=time.time(),
                resolution_corrected=True,source='Existing XSR RMS size PVs; source-plane um; operator-confirmed calibration',
                fresh_shot_verified=False,note='Read time, not exposure time. Exposure freshness unverified.')


def sr_beta_values(results,monitor):
    values={}
    for axis,result in results.items():
        if axis not in ('x','y'):continue
        matches=[m for m in result['monitors'] if m['name']==monitor]
        if len(matches)==1:values[axis]=float(matches[0]['beta_m'])
    if not values or not all(math.isfinite(v) and v>0 for v in values.values()):raise ValueError('No valid measured beta fit at '+monitor)
    return values


def sr_dispersion_values(result,monitor):
    names=[m['name'] for m in result['monitors']]
    if names.count(monitor)!=1:raise ValueError('Dispersion fit has no unique monitor '+monitor)
    index=names.index(monitor)
    values={a:float(p['monitor_dispersion'][index]) for a,p in result['planes'].items() if a in ('x','y')}
    if not values or not all(math.isfinite(v) for v in values.values()):raise ValueError('Invalid SR dispersion fit')
    return values
