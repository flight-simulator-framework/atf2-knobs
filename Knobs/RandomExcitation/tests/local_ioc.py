"""Isolated caproto fixture. TEST prefix and loopback interface are mandatory."""
import math
import os
import numpy as np

from caproto.server import PVGroup, pvproperty, run


def scalar(pv):
    return float(np.asarray(pv.value).ravel()[0])


class TestIOC(PVGroup):
    phase = pvproperty(name='CM1L:phaseWrite', value=12.0)
    phase_read = pvproperty(name='CM1L:phaseRead', value=12.0, read_only=True)
    current = pvproperty(name='QA1L:currentWrite', value=3.0)
    current_read = pvproperty(name='QA1L:currentRead', value=3.0, read_only=True)
    lower = pvproperty(name='QA1L:parameter.A', value=1.2, read_only=True)
    upper = pvproperty(name='QA1L:parameter.B', value=4.8, read_only=True)
    gun = pvproperty(name='BIM:GUN:nparticles', value=4.0, read_only=True)
    ln0 = pvproperty(name='BIM:LN0:nparticles', value=3.8, read_only=True)
    lne = pvproperty(name='BIM:LNE:nparticles', value=3.6, read_only=True)
    btm = pvproperty(name='BIM:BTM:nparticles', value=3.4, read_only=True)
    bte = pvproperty(name='BIM:BTE:nparticles', value=3.2, read_only=True)
    dr = pvproperty(name='BIM:DR:nparticles', value=3.0, read_only=True)
    monitors = pvproperty(name='DR:monitors', value=[0.] * 980, max_length=980, read_only=True)
    mode = pvproperty(name='DRBPM:ORBIT_MODE', value=1, read_only=True)
    turn = pvproperty(name='DRBPM:INJ:TURN_COUNT', value=7, read_only=True)
    command_count = pvproperty(name='test:command_count', value=0, read_only=True)

    @phase.putter
    async def phase(self, instance, value):
        await self.phase_read.write(value)
        await self.command_count.write(int(scalar(self.command_count)) + 1)
        return value

    @current.putter
    async def current(self, instance, value):
        if not scalar(self.lower) <= value <= scalar(self.upper):
            raise ValueError('Current exceeds parameter.A/B')
        await self.current_read.write(value)
        await self.command_count.write(int(scalar(self.command_count)) + 1)
        return value

    @gun.scan(period=.05)
    async def gun(self, instance, async_lib):
        self.tick = getattr(self, 'tick', 0) + 1
        noise = .002 * math.sin(self.tick / 10)
        attenuation = math.exp(-((scalar(self.phase) - 12) / 100) ** 2)
        for index, pv in enumerate((self.gun, self.ln0, self.lne, self.btm, self.bte, self.dr)):
            await pv.write((4. - .2 * index) * attenuation + noise)
        waveform = []
        for i in range(98):
            waveform.extend([1., float(i), -float(i), 3. + noise, 0., 0., 0., 0., 0., 0.])
        await self.monitors.write(waveform)


if __name__ == '__main__':
    port = os.environ.get('EPICS_CA_SERVER_PORT')
    if not port or int(port) < 1024:
        raise SystemExit('Set an isolated test port first')
    ioc = TestIOC(prefix='BDXRTEST:')
    run(ioc.pvdb, interfaces=['127.0.0.1'])
