"""Adaptive, bracketed minimization of measured vertical XSR size."""
import json
import numpy as np
from scipy.stats import t


class NoXSRMinimum(ValueError):
    """A completed scan found no actionable XSR minimum."""


def historical_sem_floor(directory,machine):
    """Use recent ten-update precision and independently verified prediction errors."""
    sems=[];errors=[]
    # Filenames contain target first; order by the timestamp suffix instead.
    paths=sorted(directory.glob('bump_*.xsr.json'),key=lambda p:p.name.rsplit('_',3)[-3:],reverse=True)
    for path in paths:
        try:
            with path.open(encoding='utf-8') as f:record=json.load(f)
            if not isinstance(record,dict) or record.get('schema')!='dr-bump-xsr-history-1' or record['machine']!=machine:continue
            sem=float(record['median_sem_um']);error=float(record['verification_error_um'])
            if not np.isfinite([sem,error]).all() or sem<0:continue
        except (OSError,ValueError,KeyError,TypeError):continue
        sems.append(sem);errors.append(abs(error))
        if len(sems)==20:break
    if not sems:return 0.
    # For independent means the difference includes noise from both measurements.
    repeatability=1.4826*np.median(errors)/np.sqrt(2) if len(errors)>=3 else 0.
    return float(max(np.median(sems),repeatability))


def vertex_interval(coeff,cov,dof):
    """Invert the two-sided 95% t-test on the derivative at each displacement."""
    a,b=coeff[:2];q=t.ppf(.975,dof)**2
    polynomial=[4*(a*a-q*cov[0,0]),4*(a*b-q*cov[0,1]),b*b-q*cov[1,1]]
    if a<=0 or polynomial[0]<=0:return None
    roots=np.roots(polynomial)
    if not np.isreal(roots).all():return None
    return sorted(roots.real.tolist())


def size_fit(points,bounds,sem_floor):
    x=np.array([p['amplitude_mm'] for p in points]);y=np.array([p['xsr']['mean'] for p in points])
    err=np.array([p['xsr']['sem'] for p in points])
    if len(x)<5 or len(np.unique(x))<3 or not np.isfinite(np.r_[x,y,err]).all() or (y<=0).any() or (err<0).any():
        raise ValueError('Need five valid XSR measurements for a size-squared fit')
    err=np.maximum(err,max(sem_floor,float(np.median(y))*5.e-7))
    uncertainty=2*y*err
    matrix=np.column_stack((x*x,x,np.ones(len(x))))/uncertainty[:,None]
    coeff,_,rank,_=np.linalg.lstsq(matrix,y*y/uncertainty,rcond=1.e-10)
    if rank!=3:raise ValueError('Rank-deficient XSR fit')
    residual=(np.polyval(coeff,x)-y*y)/uncertainty
    scale=max(float(residual@residual)/(len(x)-3),1.)
    covariance=np.linalg.inv(matrix.T@matrix)*scale
    a,b=coeff[:2]
    best=float(-b/(2*a)) if a>0 else None
    low=max(min(x),bounds[0]);high=min(max(x),bounds[1])
    grid=np.linspace(low,high,201);size2=np.polyval(coeff,grid)
    if np.any(size2<=0):raise ValueError('Unphysical fitted XSR size')
    bracketed=best is not None and low<best<high
    ci=vertex_interval(coeff,covariance,len(x)-3)
    i,j=int(np.argmin(y)),int(np.argmax(y))
    flat=y[j]-y[i]<=2*np.hypot(err[i],err[j])
    return dict(coefficients_size_squared=coeff,covariance=covariance,best_mm=best,
                predicted_size_um=float(np.sqrt(np.polyval(coeff,best))) if bracketed else None,
                bracketed=bracketed,flat=bool(flat),ci95_mm=ci,point_count=len(x),
                curvature_resolved=bool(a>2*np.sqrt(covariance[0,0])),covariance_scale=scale,
                typical_sem_um=float(np.median(err)),sem_floor_um=sem_floor,
                amplitude_mm=grid,size_um=np.sqrt(size2),weighted_residual=residual,
                method='weighted quadratic in sigma_y squared')


def fit_size(points,amplitude_mm,bounds_mm=None):
    """Strict fit used by callers requesting a resolved, bracketed minimum."""
    fit=size_fit(points,(-amplitude_mm,amplitude_mm) if bounds_mm is None else bounds_mm,
                 float(np.median([p['xsr']['sem'] for p in points])) if points else 0.)
    if not fit['curvature_resolved']:raise ValueError('XSR fit has no resolved positive curvature')
    if not fit['bracketed']:raise ValueError('XSR minimum is not bracketed inside the measured scan range')
    return fit


def next_xsr_point(fit,points,selected,step,blocked):
    """Return the next displacement and phase, or None to verify the fitted minimum."""
    x=np.array([p['amplitude_mm'] for p in points])
    requested=np.array([p['requested_amplitude_mm'] for p in points])
    low,high=selected['bounds_mm'];center=(low+high)/2
    if fit['flat']:raise NoXSRMinimum('Flat XSR scan: no improvement resolved above measurement noise')
    best=fit['best_mm'];ci=fit['ci95_mm'];bracketed=fit['bracketed']
    # For a monotone scan, the best observed endpoint determines which side to test.
    edge_estimate=best if best is not None else x[np.argmin([p['xsr']['mean'] for p in points])]
    side=1 if edge_estimate>center else -1
    edge=high if side==1 else low
    near_edge=side*(edge-edge_estimate)<=min(step,(high-low)/8)+1.e-10
    expanded=any(p['phase']=='edge' for p in points)
    def new_point(candidate):
        return not np.any(np.isclose(candidate,np.r_[requested,blocked],atol=1.e-6,rtol=0.))
    if near_edge and (not expanded or not bracketed) and len(points)<7:
        candidate=center+side*.5
        # Preserve COD/current vetoes from window discovery, including interior failures.
        obstructed=any(min(edge,candidate)-1.e-9<=p<=max(edge,candidate)+1.e-9 for p in blocked)
        if not expanded and high-low<1.-1.e-9 and new_point(candidate) and not obstructed:
            return float(candidate),'edge'
        # Measure between the two outer points when further expansion is unavailable.
        outer=np.sort(requested)
        candidate=float(np.mean(outer[-2:] if side==1 else outer[:2]))
        if new_point(candidate) and not any(p['phase']=='edge check' for p in points):
            return candidate,'edge check'
    precise=bracketed and ci is not None and ci[0]>=min(x) and ci[1]<=max(x) and max(best-ci[0],ci[1]-best)<=.05
    if precise:return None
    if len(points)>=7 or (expanded and not bracketed):
        if bracketed and fit['curvature_resolved'] and ci is not None and min(x)<=ci[0]<=ci[1]<=max(x):
            return None
        raise NoXSRMinimum('XSR minimum not bracketed or unresolved within the measured range')
    if not bracketed:
        raise NoXSRMinimum('XSR minimum not bracketed inside the measured range')
    lo=min(requested);hi=max(requested)
    candidates=[float(v) for v in (best-step,best+step) if lo<=v<=hi and new_point(v)]
    if not candidates:
        ordered=np.sort(np.unique(requested))
        candidates=[float(v) for v in (ordered[:-1]+ordered[1:])/2 if new_point(v)]
    if not candidates:raise NoXSRMinimum('No distinct refinement point inside the measured range')
    # Rank the candidate by the predicted vertex interval after one more
    # independent sample, assuming the present quadratic and typical SEM.
    cov=fit['covariance'];coeff=fit['coefficients_size_squared']
    def score(v):
        h=np.array([v*v,v,1.]);ch=cov@h
        variance=4*np.polyval(coeff,v)*fit['typical_sem_um']**2*fit['covariance_scale']
        updated=cov-np.outer(ch,ch)/(variance+h@ch)
        interval=vertex_interval(coeff,updated,len(points)-2)
        # A finite derivative-variance criterion breaks ties for unbounded intervals.
        slope=np.array([2*best,1.,0.])
        return (interval[1]-interval[0] if interval else np.inf,float(slope@updated@slope))
    return min(candidates,key=score),'refinement'
