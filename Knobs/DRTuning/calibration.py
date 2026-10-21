"""Two-coil mag_ki_main binding, preserving the legacy main/trim convention."""
import ctypes
import numpy as np


def coil_k1l(wrapper,name,energy_GeV,main_A,trim_A=0.,coil='main'):
    if coil not in ('main','trim') or not np.isfinite([energy_GeV,main_A,trim_A]).all():raise ValueError('Invalid coil calibration request')
    kval=(ctypes.c_float*2)(0.,0.);curr=(ctypes.c_float*2)(float(main_A),float(trim_A))
    field=(ctypes.c_float*2)(0.,0.);length=ctypes.c_float()
    status=wrapper._func(2,name.encode('ascii'),float(energy_GeV),kval,curr,ctypes.byref(length),field)
    if status%2!=1:raise RuntimeError('mag_ki_main failed for '+name)
    value=float(kval[1 if coil=='trim' else 0])
    if not np.isfinite(value):raise ValueError('Nonfinite calibrated K1L')
    return value
