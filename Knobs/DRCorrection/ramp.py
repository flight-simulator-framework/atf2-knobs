"""DeltaFRamp switch adapter, using the same controller PVs as SysID.

The controller performs the linked machine operation. This client never writes
FREQUENCY:*_write or cooling-water PVs itself. Raw frequency readback is MHz;
PLn/MIn labels are kHz offsets. No real connection is made on import.
"""
import time
import numpy as np


class _PresetTransition(RuntimeError):
    """Binary preset status bits do not yet identify a unique selection."""


class DeltaRamp:
    def __init__(self,get,put,config):
        self.get=get;self.put=put;self.config=config;self.report=lambda message:None
    def trace(self,message):self.report('DeltaFRamp: '+message)
    def command(self,pv,value):
        self.trace(f'command {pv}={value}')
        self.put(pv,value)
    @staticmethod
    def label(offset):
        if not np.isfinite(offset) or abs(offset)>10000 or offset%1000!=0:
            raise ValueError('DeltaFRamp requires integer kHz presets within +/-10 kHz')
        return 'PM0' if offset==0 else ('PL' if offset>0 else 'MI')+str(int(abs(offset)/1000))
    def state(self):
        enabled=float(self.get('RAMP:STATUS_READ'))
        if enabled not in (0.,1.):raise RuntimeError('Invalid RAMP status')
        if not enabled:return dict(enabled=False,offset_hz=0.)
        if float(self.get('RAMP:HOLD:STATUS_READ'))!=0:raise RuntimeError('Release DeltaFRamp HOLD before scanning')
        selected=[]
        for offset in range(-10000,10001,1000):
            value=float(self.get('RAMP:'+self.label(offset)+':STATUS_READ'))
            if value not in (0.,1.):raise RuntimeError('Invalid ramp preset status')
            if value==1.:selected.append(offset)
        if len(selected)!=1:raise _PresetTransition('DeltaFRamp preset is not uniquely selected: '+str(selected))
        return dict(enabled=True,offset_hz=float(selected[0]))
    def preset_matches(self,target):
        # A sequence of separate CA reads can straddle a preset transition.
        # Only this condition is retryable; HOLD, invalid and disconnected PVs
        # still fail, and snapshots before commands remain strict.
        try:return self.state()==target
        except _PresetTransition:return False
    def read_hz(self):
        if self.config.get('read_process_pv'):self.put(self.config['read_process_pv'],1)
        value=float(self.get(self.config['read']))*self.config['hz_per_unit']
        if not np.isfinite(value):raise RuntimeError('Nonfinite frequency readback')
        return value
    def snapshot(self):return dict(self.state(),read_hz=self.read_hz())
    def diagnostics(self,extra_pvs=()):
        # Read only: preserve evidence before rollback, without issuing another
        # command or processing the frequency record. Diagnostics must not mask
        # the original failure if a PV is disconnected.
        values=[]
        for pv in ('RAMP:CONTROL_ON_SW','RAMP:STATUS_READ','RAMP:HOLD:STATUS_READ',self.config['read'],*extra_pvs):
            try:value=self.get(pv)
            except Exception as error:value='unavailable ('+str(error)+')'
            values.append(pv+'='+str(value))
        try:values.append('selected_state='+str(self.state()))
        except Exception as error:values.append('selected_state unavailable ('+str(error)+')')
        return '; '.join(values)
    def wait(self,predicate,description,diagnostic_pvs=()):
        self.trace('waiting for '+description)
        deadline=time.monotonic()+self.config['timeout_s']
        while not predicate():
            if time.monotonic()>deadline:
                raise RuntimeError('DeltaFRamp timeout: '+description+'; before restoration: '+self.diagnostics(diagnostic_pvs))
            time.sleep(.05)
        self.trace('confirmed '+description)
    def wait_frequency(self,expected,description,context=''):
        last=None
        def matched():
            nonlocal last
            last=self.read_hz()
            return abs(last-expected)<=self.config['tolerance_hz']
        try:self.wait(matched,description)
        except RuntimeError as error:
            # Keep the last checked value, not a new read after restoration.
            details=f'expected_hz={expected:.6f}; tolerance_hz={self.config["tolerance_hz"]}'
            if last is not None:details+=f'; observed_hz={last:.6f}; error_hz={last-expected:+.6f}'
            if context:details+='; '+context
            raise RuntimeError(str(error)+'; '+details) from error
    def _apply(self,state):
        if not state['enabled']:
            # SysID reset_energy uses value 0 on CONTROL_OFF_SW.
            self.command('RAMP:CONTROL_OFF_SW',0)
            self.wait(lambda:float(self.get('RAMP:STATUS_READ'))==0,'OFF')
        else:
            current=self.state()  # Also reject HOLD before issuing a command.
            if not current['enabled']:
                self.command('RAMP:CONTROL_ON_SW',1)
                self.wait(lambda:float(self.get('RAMP:STATUS_READ'))==1,'ON')
                # op-base mOTRScanner waits 1 s after RAMP ON before selecting
                # a preset. ON status alone need not mean it is ready for one.
                time.sleep(self.config.get('enable_settle_s',1.))
                if not self.state()['enabled']:raise RuntimeError('DeltaFRamp turned OFF during enable settling')
            else:self.trace('already ON; selecting preset without another ON command')
            label=self.label(state['offset_hz']);self.command('RAMP:'+label+':ONOFF_SW',1)
            self.wait(lambda:self.preset_matches(dict(enabled=True,offset_hz=state['offset_hz'])),'preset '+label,
                      ('RAMP:'+label+':ONOFF_SW','RAMP:'+label+':STATUS_READ'))
    def restore(self,state):
        self.trace('restoring '+str(state))
        self._apply(state)
        if 'read_hz' in state:self.wait_frequency(state['read_hz'],'restored RF frequency')
        time.sleep(self.config['settle_s'])
        if 'read_hz' in state and abs(self.read_hz()-state['read_hz'])>self.config['tolerance_hz']:raise RuntimeError('Restored RF frequency did not remain settled')
        self.trace('restoration complete')
    def set(self,offset):
        self.label(offset)  # Reject unsupported sub-kHz targets before writes.
        old=self.snapshot();before=old['read_hz']
        target=dict(enabled=True,offset_hz=float(offset))
        expected=before+offset-old['offset_hz']
        self.trace(f'request {old} -> {target}; expected_hz={expected:.6f}')
        try:
            self._apply(target)
            self.wait_frequency(expected,'RF frequency',
                                f'initial_hz={before:.6f}; initial_enabled={old["enabled"]}; '
                                f'initial_offset_hz={old["offset_hz"]}; target_offset_hz={offset}')
            time.sleep(self.config['settle_s'])
            if abs(self.read_hz()-expected)>self.config['tolerance_hz']:raise RuntimeError('RF frequency did not remain settled')
            self.trace(f'target settled: offset_hz={offset}; expected_hz={expected:.6f}')
        except Exception as error:
            self.trace('failed before restoration: '+str(error))
            try:self.restore(old)
            except Exception as restore_error:
                self.trace('restoration FAILED: '+str(restore_error))
                raise RuntimeError(str(error)+'; restoration also failed: '+str(restore_error)) from error
            raise
