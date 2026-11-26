"""Signal to noise of a measured magnet response on enabled BPMs."""
import numpy as np


def mean_variance(frame,axis):
    count=np.asarray(frame.get('valid_sample_count',frame['nshots']))
    std=np.asarray(frame['std'+axis],float)
    return np.divide(std**2,count-1,out=np.full_like(std,np.nan),where=count>1)


def response_quality(signal,noise,target):
    """RMS endpoint response and standard error in the same units."""
    if not np.isfinite([signal,noise,target]).all() or noise<0 or target<=0:
        raise ValueError('Invalid probe S/N estimate')
    return dict(signal_rms=float(signal),noise_rms=float(noise),snr=float(signal/noise) if noise>0 else None,
                target_snr=target,usable=bool(signal>1.e-12 and signal>=target*noise))


def wider_probe(width,quality,limit):
    snr=quality['snr']
    factor=min(3.,1.3*quality['target_snr']/snr) if snr and snr>0 else 3.
    return min(limit,width*factor)
