"""Two-coil mag_ki_main binding, preserving the legacy main/trim convention."""
import ctypes
import numpy as np
import hashlib
from pathlib import Path
import re
import subprocess


def build_library():
    """Reuse installed machine-interfaces calibration sources, not an agent path."""
    from machine_interfaces_atf2 import MagKi
    directory=Path(MagKi.__file__).parent/'libmagnet'
    names=re.findall(r'^OBJS \+= (mag_ki_\w+)\.o$',(directory/'Makefile').read_text(),re.M)
    sources=[directory/(name+'.c') for name in names]
    if not sources:raise RuntimeError('Installed magnet calibration sources are unavailable')
    digest=hashlib.sha256(b''.join(p.read_bytes() for p in sources)).hexdigest()[:12]
    output=Path('/tmp')/f'atf-observable-magnet-{digest}.so'
    if not output.exists():
        subprocess.run(['gcc','-shared','-fPIC','-O2','-o',str(output),*map(str,sources),'-lm'],check=True,capture_output=True)
    return output


class MagnetCalibration:
    def __init__(self):
        from machine_interfaces_atf2.MagKi import MagKiWrapper
        self.mag=MagKiWrapper(build_library())
    def corrector_tmm_per_A(self,name):
        result=self.mag._call(2,name,1.3,current_main=1.)
        return result['field']*result['efflen']*1000
    def quad_current(self,name,k1l):return self.mag.k1l_to_current(name,k1l,1.3)


def coil_k1l(wrapper,name,energy_GeV,main_A,trim_A=0.,coil='main'):
    if coil not in ('main','trim') or not np.isfinite([energy_GeV,main_A,trim_A]).all():raise ValueError('Invalid coil calibration request')
    kval=(ctypes.c_float*2)(0.,0.);curr=(ctypes.c_float*2)(float(main_A),float(trim_A))
    field=(ctypes.c_float*2)(0.,0.);length=ctypes.c_float()
    status=wrapper._func(2,name.encode('ascii'),float(energy_GeV),kval,curr,ctypes.byref(length),field)
    if status%2!=1:raise RuntimeError('mag_ki_main failed for '+name)
    value=float(kval[1 if coil=='trim' else 0])
    if not np.isfinite(value):raise ValueError('Nonfinite calibrated K1L')
    return value
