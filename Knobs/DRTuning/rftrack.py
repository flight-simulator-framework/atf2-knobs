"""Native RF-Track transverse DR diagnostics. No claim of radiation equilibrium.

Public reads are BPM signals and magnet currents. Periodic-orbit solving is
inside the instrument simulation; it is never used as the beta measurement.
"""
from pathlib import Path
import numpy as np
from Knobs.DRCorrection.backends import DemoBackend
from Knobs.DRCorrection.tbt import analyze_tbt
from .hardware import QM_MAIN_CANDIDATES,quadrupole_supply
from .calibration import coil_k1l,MagnetCalibration


class RFTrackBackend(DemoBackend):
    label='RFTrack — native transverse DR (no radiation equilibrium)'
    sr_calibration=None
    nominal_frequency_hz=714e6
    def __init__(self,seed=32):
        import RF_Track as rft
        from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_lattice import build_atf_dr_lattice
        from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_correction import ATFDRRingCorrection
        rft.number_of_threads=1
        self.cal=MagnetCalibration();self.lattice=build_atf_dr_lattice(rf_mode='disabled')
        self.solver=ATFDRRingCorrection(self.lattice,population=1e9)
        self.identity=f'rftrack-native-dr-{seed}';self.rng=np.random.default_rng(seed)
        self.offset=0.;self.acquisitions=0;self.noise_mm=.001
        self.bpm_names=['dr:'+n for n in self.solver.bpm_names]
        self.hcorrectors=['dr:'+n for n in self.solver.get_corrector_names('x')]
        self.vcorrectors=['dr:'+n for n in self.solver.get_corrector_names('y')]
        self.currents={n:0. for n in self.hcorrectors+self.vcorrectors}
        self.kick_cal={n:self.cal.corrector_tmm_per_A(n[3:])/(-1299.9999)*.299792458 for n in self.currents}
        self.skews=list(self.solver.get_skew_corrector_names());self.skew_values={n:0. for n in self.skews}
        self.quadrupoles={};self.qelements={};self.qcurrents={};self.qreference={}
        self.main_quadrupoles={};self.main_currents={}
        for e in self.lattice.get_quadrupoles():
            n=e.get_name()
            if n in self.quadrupoles:continue
            base='QM7AR' if n=='QM7R.1' else n.split('.')[0]
            try:
                k=float(e.get_K1L(-1299.9999));current=self.cal.quad_current(base,k)
                if not np.isfinite(current):continue
                self.quadrupoles[n]=dict(calibration_name=base,main_A=current,limits_A=[-2.,2.],
                    family='QF1R' if n.startswith('QF1R.') else 'QF2R' if n.startswith('QF2R.') else
                    'QMTRIM2' if n.endswith('.2') else 'QMTRIM1',unit='A',
                    note='mag_ki_main trim calibration; +/-2 A simulation limit')
                self.qelements[n]=e;self.qcurrents[n]=0.
                self.qreference[n]=(0.,k,self.quadrupole_k1l(n,0.))
            except RuntimeError:continue
        for family in QM_MAIN_CANDIDATES:
            members=(family+'.1',family+'.2')
            if all(n in self.quadrupoles for n in members):
                reference=self.quadrupoles[members[0]]['main_A']
                self.main_quadrupoles[family]=dict(members=members,reference_A=reference,
                    limits_A=[reference-3.,reference+3.])  # Simulator range around the design current.
                self.main_currents[family]=reference
        self.positions_m=np.arange(len(self.bpm_names),dtype=float)
        try:
            from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_twiss import load_atf_dr_twiss
            rows=load_atf_dr_twiss();lookup={r['NAME']:float(r['S']) for r in rows}
            self.positions_m=np.array([lookup[n[3:]] for n in self.bpm_names])
        except (KeyError,ValueError):pass
        self._target={'x':np.zeros(len(self.bpm_names)),'y':np.zeros(len(self.bpm_names))}
        self.has_horizontal_dispersion_target=False
    def target_dispersion(self,plane):
        if plane=='x' and not self.has_horizontal_dispersion_target:raise ValueError('Load public nominal horizontal dispersion')
        return self._target[plane].copy()
    def write(self,kind,names,values):
        super().write(kind,names,values)
        for n,value in zip(names,values):
            if kind=='skews':self.solver.set_skew_strength(n,float(value))
            else:
                kick=[0.,0.];kick[int(n in self.vcorrectors)]=self.kick_cal[n]*value
                self.solver._set_corrector_kick(self.solver._single_element(n[3:]),kick)
    def snapshot(self):
        out=super().snapshot();out['quadrupoles']=dict(self.qcurrents)
        out['quadrupoles'].update({n:v for n,v in self.main_currents.items() if self.main_current_delta(n)})
        return out
    def restore(self,snapshot):
        super().restore(snapshot)
        if 'quadrupoles' in snapshot:
            saved=snapshot['quadrupoles']
            for n in self.main_quadrupoles:
                if n not in saved and self.main_current_delta(n):
                    self.set_quadrupole_current(n,self.main_quadrupoles[n]['reference_A'])
            for n,v in saved.items():self.set_quadrupole_current(n,v)
    def quadrupole_current(self,name):
        return self.main_currents[name] if name in self.main_quadrupoles else self.qcurrents[name]
    def main_current_delta(self,family):
        if family not in self.main_quadrupoles:return 0.
        return self.main_currents[family]-self.main_quadrupoles[family]['reference_A']
    def quadrupole_k1l(self,name,current):
        if name in self.main_quadrupoles:return coil_k1l(self.cal.mag,name,1.3,current)
        p=self.quadrupoles[name];mag=p['calibration_name']
        return coil_k1l(self.cal.mag,mag,1.3,p['main_A']+self.main_current_delta(mag),current,'trim')
    def validate_quadrupole_targets(self,name,values):
        low,high=quadrupole_supply(self,name)['limits_A']
        if not np.isfinite(values).all() or np.any(np.asarray(values)<low) or np.any(np.asarray(values)>high):
            raise ValueError('Quadrupole outside simulated current limits')
    def set_quadrupole_current(self,name,value):
        self.validate_quadrupole_targets(name,[value])
        main=self.main_quadrupoles.get(name)
        members=main['members'] if main else (name,)
        strengths=[]
        for member in members:
            p=self.quadrupoles[member];mag=p['calibration_name'];design=p['main_A']
            shift=value-main['reference_A'] if main else self.main_current_delta(mag)
            trim=self.qcurrents[member] if main else value
            _,k0,trim0=self.qreference[member]
            main_change=coil_k1l(self.cal.mag,mag,1.3,design+shift)-coil_k1l(self.cal.mag,mag,1.3,design) if shift else 0.
            trim_change=coil_k1l(self.cal.mag,mag,1.3,design+shift,trim,'trim')-trim0
            strengths.append(k0+main_change+trim_change)
        for member,strength in zip(members,strengths):
            e=self.qelements[member];e.set_K1(-1299.9999,strength/e.get_length())
        currents=self.main_currents if main else self.qcurrents
        currents[name]=float(value)
    def _momentum(self):return 1299.9999*(1-self.offset/(714e6*.0021))
    def orbit(self):
        result=self.solver.find_closed_orbit(momentum_mev_c=self._momentum())
        self.acquisitions+=1
        return dict(names=self.bpm_names,x=result.x+self.rng.normal(0,self.noise_mm,len(self.bpm_names)),
            y=result.y+self.rng.normal(0,self.noise_mm,len(self.bpm_names)),tmit=np.full(len(self.bpm_names),1e9),
            valid=np.ones(len(self.bpm_names),bool),position_unit='mm',acquisition=self.acquisitions)
    def tbt(self,turns=256,pulse=None,cancel=None):
        if not isinstance(turns,int) or not 16<=turns<=1024:raise ValueError('TBT: 16..1024 turns')
        momentum=self._momentum();orbit=self.solver.find_closed_orbit(momentum_mev_c=momentum)
        state=orbit.initial_coordinates.copy();frames=[]
        if pulse:
            n=pulse['corrector'];value=float(pulse['delta_A']);turn=int(pulse['turn'])
            if n not in self.currents or abs(self.currents[n]+value)>5 or not 1<=turn<=turns:
                raise ValueError('Invalid excitation or current limit')
        for i in range(1,turns+1):
            if cancel is not None and cancel.is_set():raise RuntimeError('TBT cancelled')
            active=pulse is not None and i==turn
            old=self.currents[n] if active else None
            try:
                if active:self.write('correctors',[n],[old+value])
                state=self.solver._track_coordinates(state,momentum)
                frames.append(self.solver._bpm_positions(None)[1])
            finally:
                if active:self.write('correctors',[n],[old])
        frames=np.asarray(frames);shape=frames.shape[:2];self.acquisitions+=1
        return dict(names=self.bpm_names,turns=list(range(1,turns+1)),
            x=frames[:,:,0]+self.rng.normal(0,self.noise_mm,shape),
            y=frames[:,:,1]+self.rng.normal(0,self.noise_mm,shape),tmit=np.full(shape,1e9),valid=np.ones(shape,bool),
            position_unit='mm',bpm_noise_rms_mm=self.noise_mm,equilibrium_validated=False,
            orbit_regime='native RFTrack transverse tracking',acquisition=self.acquisitions,excitation=pulse)
    def measure_tunes(self,cancel=None):
        readings=[];tunes=[]
        for axis,names in [('x',self.hcorrectors),('y',self.vcorrectors)]:
            # Actual BPM waveforms, no eigenvalues/Twiss supplied to the analyzer.
            wave=self.tbt(256,dict(corrector=names[0],delta_A=.1,turn=1),cancel=cancel)
            fit=analyze_tbt(wave,start_turn=3)['planes'][axis]
            if not fit['valid']:raise RuntimeError('No measurable '+axis+' tune')
            tunes.append(fit['tune']);readings.append(dict(axis=axis,tune=fit['tune'],acquisition=wave['acquisition']))
        return dict(tunes=tunes,source='two excited native RFTrack BPM waveforms',readings=readings)
