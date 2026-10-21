"""File adapters for op-base orbit buffers and response-based SR fits.

SAD input is parsed as data; no SAD/Python expressions are executed. The M.i
mapping must be provided explicitly because it is a lattice ordering, not a PV.
"""
import json
import re
from pathlib import Path
import numpy as np
from .core import checked_orbit, serial


def parse_sad_orbits(text, bpm_mapping):
    number=r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:(?:[eE]|\*\^)[+-]?\d+)?'
    row=re.compile(r'\{\s*"([^"\n]+)"\s*,\s*('+number+r')\s*,\s*('+number+r')\s*,\s*('+number+r')\s*\}')
    frames=[]
    for match in re.finditer(r'ATFBPM\s*->\s*\{',text):
        start=match.end()-1;depth=0;end=None
        for i in range(start,len(text)):
            if text[i]=='{':depth+=1
            elif text[i]=='}':depth-=1
            if depth==0:end=i+1;break
        if end is None:raise ValueError('Unterminated ATFBPM block')
        rows=row.findall(text[start+1:end-1])
        if not rows:raise ValueError('No numeric BPM rows in ATFBPM block')
        remainder=row.sub('',text[start+1:end-1]).strip(' \t\n\r,')
        if remainder:raise ValueError('Unsupported SAD expression in BPM data')
        names=[];values=[]
        for name,*xyz in rows:
            if name not in bpm_mapping:raise ValueError('Missing explicit SAD BPM mapping: '+name)
            names.append(bpm_mapping[name]);values.append([float(x.replace('*^','e')) for x in xyz])
        if len(set(names))!=len(names):raise ValueError('Duplicate BPM mapping')
        values=np.asarray(values)
        frames.append(dict(schema='dr-orbit-1',names=names,x=values[:,0]*1000,y=values[:,1]*1000,
            tmit=values[:,2],valid=np.isfinite(values).all(axis=1)&(values[:,2]>0),position_unit='mm',source='SAD ATFBPM; source positions in m'))
    if not frames:raise ValueError('No ATFBPM block found')
    return frames


def aligned_difference(a,b):
    """Return B-A, matching BPM names rather than assuming identical ordering."""
    if a.get('position_unit')!='mm' or b.get('position_unit')!='mm':raise ValueError('Orbit units must be mm')
    ax,ay,valid=checked_orbit(a,a['names']);bx,by,bvalid=checked_orbit(b,b['names'])
    if len(set(a['names']))!=len(a['names']) or set(a['names'])!=set(b['names']):raise ValueError('BPM name sets differ')
    order=[b['names'].index(n) for n in a['names']]
    return dict(schema='dr-orbit-1',names=list(a['names']),x=bx[order]-ax,y=by[order]-ay,
        tmit=np.minimum(a['tmit'],np.asarray(b['tmit'])[order]),valid=valid&bvalid[order],position_unit='mm',source='Buffer B minus A')


def load_tbt_text(paths,names,*,mm_per_unit,noise_mm):
    """Legacy three whitespace matrices (X,Y,intensity), each [turn,BPM]."""
    arrays={}
    for key in ('x','y','tmit'):
        first=Path(paths[key]).read_text().splitlines()[0].split()
        # Turn1024 exports an un-commented bpm01_x ... bpm96_x header.
        header=bool(first) and all(re.fullmatch(r'bpm\d+_(?:x|y|wint)',token) for token in first)
        arrays[key]=np.loadtxt(paths[key],ndmin=2,skiprows=int(header))
    shape=arrays['x'].shape
    if shape[1]!=len(names) or shape[0]<16 or any(v.shape!=shape for v in arrays.values()):raise ValueError('TBT matrices must share [turn, BPM] shape')
    if not np.isfinite(mm_per_unit) or mm_per_unit<=0:raise ValueError('Positive mm/unit calibration required')
    return dict(names=list(names),turns=list(range(1,shape[0]+1)),x=arrays['x']*mm_per_unit,
        y=arrays['y']*mm_per_unit,tmit=arrays['tmit'],valid=np.isfinite(arrays['x'])&np.isfinite(arrays['y'])&(arrays['tmit']>0),
        bpm_noise_rms_mm=float(noise_mm),position_unit='mm',orbit_regime='imported TBT text',equilibrium_validated=False)


def fit_sr(data,calibration,rcond=.04):
    """Fit public response basis at BPMs; evaluate the same basis at SR monitors.

    This is the Python analogue of fitting a corrector basis in dispersion.n.
    It does not read hidden simulation optics or infer a transport matrix from s.
    Matrix rows correspond to BPMs/monitors; columns are a common basis. All
    dispersion values are mm/(dp/p), and basis coefficients retain calibration
    units. A public lattice response export supplies the calibration.
    """
    if data.get('mode')!='dispersion' or 'full_measured' not in data:raise ValueError('An X/Y dispersion session is required')
    if calibration.get('schema')!='dr-sr-response-1':raise ValueError('Expected dr-sr-response-1 calibration')
    if len(set(calibration['names']))!=len(calibration['names']):raise ValueError('Duplicate calibration BPMs')
    if not 0<=rcond<1:raise ValueError('Invalid SVD cutoff')
    index=[calibration['names'].index(n) for n in data['names']]
    valid=np.asarray(data['valid'],bool);values=np.asarray(data['full_measured'],float)[:2]
    result=dict(schema='dr-sr-fit-1',names=data['names'],positions_m=data['positions_m'],valid=valid,
        measured=values,monitors=calibration['monitors'],calibration_source=calibration.get('source','unspecified'),planes={})
    for row,plane in enumerate(('x','y')):
        c=calibration['planes'][plane];matrix=np.asarray(c['bpm_response'],float)[index]
        at_sr=np.asarray(c['monitor_response'],float)
        nominal=np.asarray(c.get('nominal_bpm',np.zeros(len(calibration['names']))),float)[index]
        nominal_sr=np.asarray(c.get('nominal_monitor',np.zeros(len(calibration['monitors']))),float)
        if matrix.ndim!=2 or at_sr.shape!=(len(calibration['monitors']),matrix.shape[1]) or not all(np.isfinite(v).all() for v in (matrix,at_sr,nominal,nominal_sr)):
            raise ValueError('Invalid SR response calibration shape/value')
        mask=valid&np.isfinite(values[row])
        if mask.sum()<matrix.shape[1]:raise ValueError('Too few valid BPMs for SR response basis')
        coefficients,residuals,rank,sv=np.linalg.lstsq(matrix[mask],values[row,mask]-nominal[mask],rcond=rcond)
        if rank<matrix.shape[1]:raise ValueError('SR fit is rank deficient; reduce the response basis')
        fitted=nominal+matrix@coefficients
        result['planes'][plane]=dict(coefficients=coefficients,rank=int(rank),singular_values=sv,fitted=fitted,
            monitor_dispersion=nominal_sr+at_sr@coefficients,rms_residual_mm=float(np.sqrt(np.mean((fitted[mask]-values[row,mask])**2))))
    return result
