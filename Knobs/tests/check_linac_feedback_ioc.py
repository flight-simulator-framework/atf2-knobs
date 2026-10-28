"""Production feedback against a disposable, prefixed loopback caproto IOC.

Run separately before initializing CA. Requires caproto, pyepics and libca.
No accelerator channels or default CA network discovery are used.
"""
import json
import csv
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import threading
from dataclasses import replace

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
        beam = pvproperty(name="test:beam", value=1.)
        bpm_valid = pvproperty(name="test:bpm_valid", value=1.)
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
                waveform[LINAC_MONITORS.index(name), :4] = [scalar(self.bpm_valid), orbit[i] * 1000, orbit[i + 2] * 1000, 1.]
            await instance.write(waveform.ravel())
            timing = (scalar(self.timing) - scalar(self.timing_target)) / 2.8
            phase = (scalar(self.phase) - scalar(self.phase_target)) / .5
            transmission = max(0., .95 - .001 * timing ** 2 - .003 * phase ** 2 - .3 * np.dot(residual, residual))
            await self.ln0.write(1.)
            await self.dr.write(transmission if scalar(self.beam) > 0 else 0.)

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
                from Knobs.linac_feedback import FeedbackConfig, FeedbackController, OrbitDrift, TIMING_PV, PHASE_PV, CURRENT_PVS
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
                # The display must keep updating without a control measurement.
                first = len(events)
                machine.wait(.8)
                assert any(e["kind"] == "transmission_monitor" for e in events[first:])
                assert not any(e["kind"] in ("transmission", "command") for e in events[first:])
                for name, value in (("test:timing_target", 105.97), ("test:drift", .2)):
                    assert factory(name).put(value, wait=True) == 1
                # Drift must wake the wait independently of the transmission test.
                controller.config = replace(config, interval_sec=5.)
                wait_start = time.monotonic()
                try:
                    controller.wait_cycle()
                    raise AssertionError("BPM drift should interrupt the cycle wait")
                except OrbitDrift:
                    assert time.monotonic() - wait_start < 5.
                assert factory("test:phase_target").put(2.5, wait=True) == 1
                controller.cycle()
                assert abs(machine.read(TIMING_PV) - 105.97) < .001
                assert abs(machine.read(PHASE_PV) - 2.5) < .001
                decision = next(e for e in events if e["kind"] == "degradation_decision" and e["degraded"])
                assert len(decision["samples"]) == 5 and decision["orbit_normal"]
                assert abs(decision["threshold"] - (controller.initial_transmission - .05)) < 1e-9
                residual = machine.orbit() - controller.reference
                assert np.max(abs(residual)) < .02, residual
                assert any(e["kind"] == "response" and e["observations"] > 4 for e in events)
                assert not any(e["kind"] == "response_probe_size" or e.get("state") == "Learning corrector response" for e in events)
                for event in events:
                    if event["kind"] == "command":
                        for pv, target in event["settings"].items():
                            if pv in CURRENT_PVS:
                                assert abs(target - event["previous"][pv]) <= .010000001
                    if event["kind"] == "orbit_samples":
                        assert len(event["timestamps"]) == len(set(event["timestamps"])) == 10
                    if event["kind"] == "ict_samples":
                        assert len(event["samples"]) == 10
                        for channel in (0, 1):
                            assert len({pair[channel] for pair in event["timestamps"]}) == 10
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
                # Exercise the real worker's automatic pause and CSV audit.
                from Knobs.linac_feedback_gui import FeedbackWorker
                beam = factory("test:beam")
                paused, monitoring = threading.Event(), threading.Event()
                bpm_invalid = threading.Event()
                injected = []
                worker_events = []
                def worker_machine(cfg, cancelled, checkpoint, emit):
                    device = FeedbackMachine(cfg, cancelled, checkpoint, emit, factory)
                    write = device.write
                    def lose_beam_after_probe(values, restore=False):
                        write(values, restore=restore)
                        if not restore and TIMING_PV in values and not injected:
                            injected.append(dict(values))
                            assert beam.put(0., wait=True) == 1
                            time.sleep(.05)
                    device.write = lose_beam_after_probe
                    return device
                worker = FeedbackWorker(config, folder, worker_machine)
                emit = worker.emit_event
                def record(event):
                    emit(event)
                    worker_events.append(event)
                    if event["kind"] == "auto_pause":
                        paused.set()
                    if event["kind"] == "bpm_invalid" and event["consecutive"] >= 5:
                        bpm_invalid.set()
                    if event["kind"] == "state" and event.get("state") == "Monitoring":
                        monitoring.set()
                worker.emit_event = record
                thread = threading.Thread(target=worker.run)
                thread.start()
                try:
                    assert paused.wait(15), worker_events[-5:]
                    assert abs(machine.scalar(TIMING_PV) - accepted_timing) < .001
                    assert beam.put(1., wait=True) == 1
                    time.sleep(.2)
                    assert worker.paused.is_set(), "Must not automatically resume when beam returns"
                    worker.paused.clear()  # operator Resume
                    assert monitoring.wait(15), worker_events[-5:]
                    paused.clear()
                    monitoring.clear()
                    assert factory("test:bpm_valid").put(0., wait=True) == 1
                    assert bpm_invalid.wait(10), worker_events[-5:]
                    assert not paused.is_set() and not worker.paused.is_set(), "BPM invalid must not auto-pause"
                    losses = [e for e in worker_events if e["kind"] in ("beam_loss_pending", "beam_loss_detected")]
                    assert [e["consecutive"] for e in losses] == list(range(1, 11)), losses
                    monitoring.clear()
                    assert factory("test:bpm_valid").put(1., wait=True) == 1
                    assert monitoring.wait(15), worker_events[-5:]
                    assert sum(e["kind"] == "auto_pause" for e in worker_events) == 1
                finally:
                    worker.stop()
                    thread.join(10)
                assert not thread.is_alive()
                assert sum(e["kind"] == "reference" for e in worker_events) == 1
                assert not any(e["kind"] == "error" for e in worker_events), worker_events[-5:]
                with (worker.run_dir / "operations.csv").open() as stream:
                    operations = list(csv.DictReader(stream))
                assert operations and all(row["TimestampISO"] and row["Before"] for row in operations)
                assert any(row["Restore"] == "True" and row["Status"] == "confirmed" for row in operations)
                print(json.dumps(dict(result="passed", timing_ns=accepted_timing,
                                      cm8l_phase_deg=machine.scalar(PHASE_PV),
                                      residual_um=(residual * 1000).tolist(), events=len(events),
                                      auto_pause_resume="passed", bpm_invalid_retry_without_pause="passed", continuous_bpm_wait="passed",
                                      live_transmission_during_wait="passed",
                                      correction_only_learning="passed",
                                      absolute_five_point_threshold="passed", operation_rows=len(operations)), indent=2))
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
