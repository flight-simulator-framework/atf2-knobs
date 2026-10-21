"""Export public DESIGN optics; never inspect the connected perturbed machine."""
import hashlib
import json
from pathlib import Path
import numpy as np


def export_phase_model(path):
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_lattice import build_atf_dr_lattice
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_twiss import _linear_map,_track_coordinates,_periodic_twiss
    import RF_Track as rft
    lattice=build_atf_dr_lattice(rf_mode='disabled');state=np.zeros(4);total=np.eye(4);maps={}
    for e in lattice['*']:
        local=rft.Lattice();local.append(e)
        m=_linear_map(local,state,1e-5);state=_track_coordinates(local,state);total=m@total
        if e.get_name().startswith('MB'):maps['dr:'+e.get_name()]=total.copy()
    names=list(maps);out=dict(schema='dr-phase-optics-1',source='Independent nominal ATF DR 20111111b RFTrack lattice, zero settings/errors',planes={})
    for axis,sl in [('x',slice(0,2)),('y',slice(2,4))]:
        tune=_periodic_twiss(total[sl,sl])[2];triplets=[]
        for i in range(len(names)-2):
            a=maps[names[i]][sl,sl];candidates=[]
            for j in range(i+1,min(i+9,len(names))):
                m=maps[names[j]][sl,sl]@np.linalg.inv(a)
                if abs(m[0,1])>.1:candidates.append((names[j],float(m[0,0]/m[0,1])))
            if len(candidates)<2:continue
            # Prefer well-conditioned cotangent difference rather than nearest BPMs.
            pairs=[(abs(u[1]-v[1]),u,v) for ii,u in enumerate(candidates) for v in candidates[ii+1:]]
            _,u,v=max(pairs)
            triplets.append(dict(names=[names[i],u[0],v[0]],m11_m12=[u[1],v[1]],phase_sign=1 if tune<.5 else -1))
        out['planes'][axis]=triplets
    content=json.dumps(out,indent=2)+'\n';Path(path).write_text(content)
    return out

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('output',type=Path);a=p.parse_args();export_phase_model(a.output)
