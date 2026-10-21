"""Feed measured arrays into the existing op-base-derived correction proposals.

Only measurement methods are overridden. No lattice is constructed, tracked or
queried; A and K1L units are carried by the measured response matrix columns.
"""
from types import SimpleNamespace
import numpy as np
from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_correction import ATFDRRingCorrection


class MeasuredProposal(ATFDRRingCorrection):
    def __init__(self,mode,plane,measured):
        self.mode=mode;self.plane=plane;self.measured=measured
    def find_closed_orbit(self,**kwargs):
        positions=np.zeros((self.measured.shape[1],2))
        positions[:,0 if self.plane=='x' else 1]=self.measured[1 if self.mode=='dispersion' else 0]
        return SimpleNamespace(bpm_positions=positions)
    def measure_dispersion(self,**kwargs):
        values=np.zeros((self.measured.shape[1],2));values[:,0 if self.plane=='x' else 1]=self.measured[0]
        return SimpleNamespace(values=values)
    def _measure_vertical_response_to_horizontal_probes(self,*args,**kwargs):return (),self.measured


def propose(data,opt,valid,available,maximum,step):
    mode=data['mode'];measured=np.asarray(data['measured'])[:,valid];target=np.asarray(data['target'])[:,valid]
    count=len(data['names']);rows=np.tile(valid,measured.shape[0]);columns=np.flatnonzero(available)
    matrix=np.asarray(data['response'])[rows][:,columns]
    names=tuple(np.array(data['names'])[valid]);actuators=tuple(np.array(data['actuators'])[columns])
    engine=MeasuredProposal(mode,opt.plane,measured)
    common=dict(rcond=opt.svd_cut,max_abs_delta=step)
    response=SimpleNamespace(plane=opt.plane,bpm_names=names,corrector_names=actuators,
        matrix=matrix,relative_momentum_step=1.,probe_corrector_names=(opt.probe1,opt.probe2),
        skew_corrector_names=actuators,probe_perturbation=1.)
    if mode=='orbit':
        result=engine.propose_orbit_correction(response,target_orbit=target[0],max_correctors=maximum,**common)
    elif mode=='dispersion':
        response.dispersion_matrix=matrix[:len(names)];response.orbit_matrix=matrix[len(names):]
        result=engine.propose_dispersion_correction(response,target_dispersion=target[0],target_orbit=target[1],
            dispersion_weight=opt.dispersion_weight,orbit_weight=opt.orbit_weight,max_correctors=maximum,**common)
    else:
        result=engine.propose_coupling_correction(response,target_signal=target.reshape(-1),**common)
    delta=np.zeros(len(data['actuators']))
    delta[columns]=result.delta_skew_correctors if mode=='skew' else result.delta_correctors
    selected=list(result.selected_skew_correctors if mode=='skew' else result.selected_correctors)
    return delta,selected,result.singular_values,result.rank
