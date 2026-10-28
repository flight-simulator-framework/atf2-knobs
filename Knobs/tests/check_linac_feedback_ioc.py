"""Production feedback against a disposable, prefixed loopback caproto IOC.

Run separately before initializing CA. Requires caproto, pyepics and libca.
No accelerator channels or default CA network discovery are used.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

PREFIX = "LINACFBTEST:"


def serve():
    import numpy as np
    from caproto.server import PVGroup, pvproperty, run
    from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS

    def scalar(instance):
        return float(np.asarray(instance.value).ravel()[0])

    class IOC(PVGroup):
        timing = pvproperty(name="EVE_LINAC:OUT0:SetData", value=100.37)
        phase = pvproperty(name="CM8L:phaseWrite", value=0.)
        phase_read = pvproperty(name="CM8L:phaseRead", value=0., read_only=True)
        timing_target = pvproperty(name="test:timing_target", value=103.17)
        phase_target = pvproperty(name="test:phase_target", value=0.)
        drift = pvproperty(name="test:drift", value=0.)
        h1 = pvproperty(name="ZH1L:currentWrite", value=0., lower_ctrl_limit=-5., upper_ctrl_limit=5.)
        h1_read = pvproperty(name="ZH1L:currentRead", value=0., read_only=True)
        v1 = pvproperty(name="ZV1L:currentWrite", value=0., lower_ctrl_limit=-5., upper_ctrl_limit=5.)
        v1_read = pvproperty(name="ZV1L:currentRead", value=0., read_only=True)
        h2 = pvproperty(name="ZH2L:currentWrite", value=0., lower_ctrl_limit=-5., upper_ctrl_limit=5.)
        h2_read = pvproperty(name="ZH2L:currentRead", value=0., read_only=True)
        v2 = pvproperty(name="ZV2L:currentWrite", value=0., lower_ctrl_limit=-5., upper_ctrl_limit=5.)
        v2_read = pvproperty(name="ZV2L:currentRead", value=0., read_only=True)
        ln0 = pvproperty(name="BIM:LN0:nparticles", value=1., read_only=True)
        dr = pvproperty(name="BIM:DR:nparticles", value=.9, read_only=True)
        monitors = pvproperty(name="LINAC:monitors", value=[0.] * (len(LINAC_MONITORS) * 20), read_only=True)

        @monitors.scan(period=.02)
        async def monitors(self, instance, async_lib):
            currents = np.array([scalar(self.h1), scalar(self.v1), scalar(self.h2), scalar(self.v2)])
            matrix = np.array([[2., .1, .3, 0.], [.3, 0., 1.5, .1],
                               [.1, 1.8, 0., .2], [0., .4, .1, 1.6]])
            drift = scalar(self.drift) * np.array([1., -.8, .9, -.7])
            residual = drift + matrix @ currents
            orbit = np.array([.5, -.2, .7, -.4]) + residual
            for write, read in ((self.phase, self.phase_read), (self.h1, self.h1_read), (self.v1, self.v1_read),
                                (self.h2, self.h2_read), (self.v2, self.v2_read)):
                await read.write(scalar(write))
            waveform = np.zeros((len(LINAC_MONITORS), 20))
            for i, name in enumerate(("ML8L", "ML10L")):
                waveform[LINAC_MONITORS.index(name), :4] = [1., orbit[i] * 1000, orbit[i + 2] * 1000, 1.]
            await instance.write(waveform.ravel())
            timing = (scalar(self.timing) - scalar(self.timing_target)) / 2.8
            phase = (scalar(self.phase) - scalar(self.phase_target)) / .5
            transmission = max(0., .95 - .001 * timing ** 2 - .003 * phase ** 2 - .3 * np.dot(residual, residual))
            await self.ln0.write(1.)
            await self.dr.write(transmission)

    run(IOC(prefix=PREFIX).pvdb, interfaces=["127.0.0.1"])


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    port, repeater = free_port(), free_port()
    os.environ.update(EPICS_CA_AUTO_ADDR_LIST="NO", EPICS_CA_ADDR_LIST=f"127.0.0.1:{port}",
                      EPICS_CA_SERVER_PORT=str(port), EPICS_CAS_SERVER_PORT=str(port),
                      EPICS_CA_REPEATER_PORT=str(repeater), EPICS_CAS_INTF_ADDR_LIST="127.0.0.1",
                      EPICS_CAS_AUTO_BEACON_ADDR_LIST="NO", EPICS_CAS_BEACON_ADDR_LIST="127.0.0.1")
    with tempfile.TemporaryDirectory(prefix="linac-feedback-ioc-") as folder:
        with (Path(folder) / "ioc.log").open("w+") as log:
            server = subprocess.Popen([sys.executable, __file__, "--ioc"], stdout=log, stderr=log)
            machine = None
            try:
                time.sleep(.7)
                if server.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read())
                import numpy as np
                from epics import PV
                from Knobs.linac_feedback import FeedbackConfig, FeedbackController, TIMING_PV, PHASE_PV
                from Knobs.linac_feedback_io import FeedbackMachine

                def factory(name, **kwargs):
                    pv = PV(PREFIX + name, **kwargs)
                    assert pv.wait_for_connection(timeout=3), name
                    return pv

                events = []
                stopped = False
                config = FeedbackConfig(settle_sec=.03, interval_sec=0., min_gain=.00001)
                machine = FeedbackMachine(config, lambda: stopped, lambda: None, events.append, factory)
                controller = FeedbackController(machine, config, machine.limits(), events.append)
                controller.initialize()
                controller.cycle()
                assert abs(machine.read(TIMING_PV) - 103.17) < .001
                for name, value in (("test:timing_target", 105.97), ("test:phase_target", 1.), ("test:drift", .2)):
                    assert factory(name).put(value, wait=True) == 1
                controller.cycle()
                assert abs(machine.read(TIMING_PV) - 105.97) < .001
                assert abs(machine.read(PHASE_PV) - 1.) < .001
                residual = machine.orbit() - controller.reference
                assert np.max(abs(residual)) < .02, residual
                assert any(e["kind"] == "response" and e["observations"] > 4 for e in events)
                for event in events:
                    if event["kind"] == "orbit_samples":
                        assert len(event["timestamps"]) == len(set(event["timestamps"])) == 3
                accepted_timing = controller.current[TIMING_PV]
                original = machine.transmission

                def cancel_during_probe():
                    nonlocal stopped
                    if abs(machine.scalar(TIMING_PV) - accepted_timing) > .001:
                        stopped = True
                    return original()

                machine.transmission = cancel_during_probe
                try:
                    controller.search(TIMING_PV, 2.8, "Stop test")
                    raise AssertionError("Expected stop during probe")
                except InterruptedError:
                    pass
                assert abs(machine.scalar(TIMING_PV) - accepted_timing) < .001
                print(json.dumps(dict(result="passed", timing_ns=accepted_timing,
                                      cm8l_phase_deg=machine.scalar(PHASE_PV),
                                      residual_um=(residual * 1000).tolist(), events=len(events)), indent=2))
            finally:
                if machine is not None:
                    machine.close()
                server.terminate()
                try:
                    server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    serve() if "--ioc" in sys.argv else main()
