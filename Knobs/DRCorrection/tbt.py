"""Native-free TBT analysis reused from simulation-agent/dr_measured_optics.py.
Source SHA256: 01cb07c4366f2b9fbe1b98cf99be5697685a3f4a2dcbce2562e56a678a60a95a
"""
import numpy as np
from scipy.optimize import minimize_scalar

def waveform_arrays(data):
    names=data["names"];turns=np.asarray(data["turns"])
    arrays={key:np.asarray(data[key],float) for key in ("x","y","tmit")}
    shape=(len(turns),len(names))
    if len(set(names))!=len(names) or any(v.shape!=shape for v in arrays.values()):
        raise ValueError("Invalid TBT axes or duplicate BPM names")
    if data.get("position_unit")!="mm" or len(turns)<1 or np.any(np.diff(turns)!=1):
        raise ValueError("Need consecutive turns and mm positions")
    valid=np.asarray(data["valid"],bool)
    if valid.shape!=shape:raise ValueError("Invalid TBT validity mask")
    valid &= (arrays["tmit"]>0)&np.isfinite(arrays["x"])&np.isfinite(arrays["y"])
    return arrays,valid,turns

def analyze_tbt(data,*,start_turn=1,stop_turn=None,reference=None):
    """Tune/phase/amplitude fit and relative beta; absolute beta needs calibration.

reference: {'x': {'bpm': 'dr:MB1R', 'beta_m': 8.}, ...}.
This is caller-supplied nominal or independently measured calibration.
"""
    arrays,valid,turns=waveform_arrays(data)
    stop_turn=int(turns[-1]) if stop_turn is None else stop_turn
    if isinstance(start_turn,bool) or int(start_turn)!=start_turn or start_turn not in turns:
        raise ValueError("Start turn outside waveform")
    if isinstance(stop_turn,bool) or int(stop_turn)!=stop_turn or stop_turn not in turns:
        raise ValueError("Stop turn outside waveform")
    select=(turns>=start_turn)&(turns<=stop_turn);n=int(select.sum())
    if n<16:raise ValueError("At least 16 turns required for tune analysis")
    good=np.all(valid[select],axis=0)
    time=np.arange(n);result=dict(names=data["names"],planes={},
        method="Hann spectral peak + sinusoidal least-squares fit; amplitude beta",
        equilibrium_validated=False,normalization="relative beta unless external reference supplied",
        tune_convention="fractional real-waveform alias in [0.02,0.48]; integer tune unavailable",
        phase_reference_turn=int(turns[select][0]),analysis_turn_window=[int(start_turn),int(stop_turn)])
    for axis in ("x","y"):
        values=arrays[axis][select].copy();values[:,~good]=0.
        centered=values-values.mean(axis=0)
        power=np.sum(abs(np.fft.rfft(centered*np.hanning(n)[:,None],axis=0))**2,axis=1)
        frequencies=np.fft.rfftfreq(n);search=(frequencies>=.02)&(frequencies<=.48)
        if not good.any() or power[search].max()<=0:
            result["planes"][axis]=dict(valid=False,reason="No valid oscillation");continue
        frequency=frequencies[np.flatnonzero(search)[np.argmax(power[search])]]
        def fit(f):
            design=np.column_stack([np.ones(n),np.cos(2*np.pi*f*time),np.sin(2*np.pi*f*time)])
            coef=np.linalg.lstsq(design,values,rcond=None)[0]
            residual=values-design@coef
            return coef,residual
        optimized=minimize_scalar(lambda f:float(np.sum(fit(f)[1][:,good]**2)),
            bounds=(max(.02,frequency-1/n),min(.48,frequency+1/n)),method="bounded",
            options={"xatol":1e-10})
        coef,residual=fit(optimized.x)
        amplitude=np.hypot(coef[1],coef[2])
        noise=np.maximum(np.std(residual,axis=0),float(data.get("bpm_noise_rms_mm",0.)))
        usable=good&(amplitude>5*noise)&np.isfinite(amplitude)
        amp=np.where(usable,amplitude,np.nan)
        plane=dict(valid=bool(usable.any()),valid_bpms=usable,tune=float(optimized.x),
            amplitude_mm=amp,phase_rad=np.where(usable,np.arctan2(-coef[2],coef[1]),np.nan),
            residual_rms_mm=np.where(good,np.std(residual,axis=0),np.nan),
            relative_beta=amp**2/np.mean(amplitude[usable]**2) if usable.any() else amp,
            beta_m=None)
        if reference and axis in reference:
            ref=reference[axis];index=data["names"].index(ref["bpm"]);beta=float(ref["beta_m"])
            if not usable[index] or not np.isfinite(beta) or beta<=0:
                raise ValueError("Invalid beta reference or unmeasurable reference BPM")
            plane["beta_m"]=beta*(amp/amplitude[index])**2
            plane["calibration"]=dict(ref,source="caller-supplied; not fitted plant truth")
        result["planes"][axis]=plane
    return result
