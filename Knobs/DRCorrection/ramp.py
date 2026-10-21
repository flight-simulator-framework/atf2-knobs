"""DeltaFRamp switch adapter, using the same controller PVs as SysID.

The controller performs the linked machine operation. This client never writes
FREQUENCY:*_write or cooling-water PVs itself. Raw frequency readback is MHz;
PLn/MIn labels are kHz offsets. No real connection is made on import.
"""
import time
import numpy as np


class DeltaRamp:
    def __init__(self,get,put,config):self.get=get;self.put=put;self.config=config
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
        if len(selected)!=1:raise RuntimeError('DeltaFRamp preset is not uniquely selected')
        return dict(enabled=True,offset_hz=float(selected[0]))
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
        deadline=time.monotonic()+self.config['timeout_s']
        while not predicate():
            if time.monotonic()>deadline:
                raise RuntimeError('DeltaFRamp timeout: '+description+'; before restoration: '+self.diagnostics(diagnostic_pvs))
            time.sleep(.05)
    def _apply(self,state):
        if not state['enabled']:
            # SysID reset_energy uses value 0 on CONTROL_OFF_SW.
            self.put('RAMP:CONTROL_OFF_SW',0)
            self.wait(lambda:float(self.get('RAMP:STATUS_READ'))==0,'OFF')
        else:
            current=self.state()  # Also reject HOLD before issuing a command.
            if not current['enabled']:
                self.put('RAMP:CONTROL_ON_SW',1)
                self.wait(lambda:float(self.get('RAMP:STATUS_READ'))==1,'ON')
                # op-base mOTRScanner waits 1 s after RAMP ON before selecting
                # a preset. ON status alone need not mean it is ready for one.
                time.sleep(self.config.get('enable_settle_s',1.))
                if not self.state()['enabled']:raise RuntimeError('DeltaFRamp turned OFF during enable settling')
            label=self.label(state['offset_hz']);self.put('RAMP:'+label+':ONOFF_SW',1)
            self.wait(lambda:self.state()==dict(enabled=True,offset_hz=state['offset_hz']),'preset '+label,
                      ('RAMP:'+label+':ONOFF_SW','RAMP:'+label+':STATUS_READ'))
    def restore(self,state):
        self._apply(state)
        if 'read_hz' in state:self.wait(lambda:abs(self.read_hz()-state['read_hz'])<=self.config['tolerance_hz'],'restored RF frequency')
        time.sleep(self.config['settle_s'])
        if 'read_hz' in state and abs(self.read_hz()-state['read_hz'])>self.config['tolerance_hz']:raise RuntimeError('Restored RF frequency did not remain settled')
    def set(self,offset):
        self.label(offset)  # Reject unsupported sub-kHz targets before writes.
        old=self.snapshot();before=old['read_hz']
        target=dict(enabled=True,offset_hz=float(offset))
        expected=before+offset-old['offset_hz']
        try:
            self._apply(target)
            self.wait(lambda:abs(self.read_hz()-expected)<=self.config['tolerance_hz'],'RF frequency')
            time.sleep(self.config['settle_s'])
            if abs(self.read_hz()-expected)>self.config['tolerance_hz']:raise RuntimeError('RF frequency did not remain settled')
        except Exception as error:
            try:self.restore(old)
            except Exception as restore_error:
                raise RuntimeError(str(error)+'; restoration also failed: '+str(restore_error)) from error
            raise
