"""Timestamp-gated acquisition. PV updates are not asserted to be shot IDs."""
import time
import numpy as np
from Knobs.DRCorrection.core import Cancelled


def updates(metadata, pvs, count, cancel, *, timeout_s=15., interval_s=0.):
    if int(count) != count or not 1 <= count <= 1000:
        raise ValueError('Sample count must be an integer in 1..1000')
    if not np.isfinite([timeout_s, interval_s]).all() or timeout_s <= 0 or interval_s < 0:
        raise ValueError('Invalid update timeout / interval')
    def read(pv):
        if cancel.is_set():raise Cancelled('Acquisition cancelled')
        r=metadata(pv)
        if r.get('value') is None or not np.isfinite(r.get('timestamp',np.nan)) or r.get('severity',0)>=2:
            raise RuntimeError('Invalid timestamp / alarm: '+pv)
        return r
    previous={pv:float(read(pv)['timestamp']) for pv in pvs}
    for _ in range(int(count)):
        if cancel.wait(interval_s):raise Cancelled('Acquisition cancelled')
        deadline=time.monotonic()+timeout_s
        while True:
            records={pv:read(pv) for pv in pvs}
            if all(float(records[pv]['timestamp'])>previous[pv] for pv in pvs):
                previous={pv:float(records[pv]['timestamp']) for pv in pvs}
                yield records
                break
            if time.monotonic()>=deadline:raise TimeoutError('No new PV update: '+', '.join(pvs))
            if cancel.wait(.02):raise Cancelled('Acquisition cancelled')


def scalar_statistics(values, timestamps, pv):
    values=np.asarray(values,float)
    if values.ndim!=1 or not len(values) or not np.isfinite(values).all() or (values<=0).any():
        raise ValueError('XSR size must be finite and positive')
    std=float(np.std(values,ddof=1)) if len(values)>1 else 0.
    return dict(pv=pv,unit='um',values=values.copy(),timestamps=list(timestamps),
                mean=float(np.mean(values)),std=std,sem=std/np.sqrt(len(values)),count=len(values),
                update_verified=True,same_shot_verified=False)
