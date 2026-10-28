"""EPICS acquisition for active feedback, owned exclusively by its worker.

Three new timestamped records are required for each mean. Repeated cached
waveforms never count as separate samples. No diagnostic selectors are written.
"""
import time

import numpy as np

from Knobs.linac_feedback import CURRENT_PVS, PHASE_PV, PVS, TIMING_PV


class FeedbackMachine:
    def __init__(self, config, cancelled, checkpoint, emit, pv_factory=None):
        if pv_factory is None:
            from epics import PV, ca
            ca.use_initial_context()
            pv_factory = PV
        self.config, self.cancelled, self._checkpoint, self.emit = config, cancelled, checkpoint, emit
        self.pv_factory, self.channels = pv_factory, {}
        self.readbacks = {pv: pv.replace("Write", "Read") for pv in CURRENT_PVS + (PHASE_PV,)}
        self.readbacks[TIMING_PV] = TIMING_PV

    def pv(self, name):
        if name not in self.channels:
            self.channels[name] = self.pv_factory(name, form="time", auto_monitor=False)
        return self.channels[name]

    def check_stop(self):
        if self.cancelled():
            raise InterruptedError("Feedback stopped")

    def checkpoint(self):
        self._checkpoint()
        self.check_stop()

    def wait(self, seconds, restore=False):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not restore:
                self.check_stop()
            time.sleep(min(.05, max(0., deadline - time.monotonic())))

    def scalar(self, name):
        raw = self.pv(name).get(use_monitor=False, timeout=1.)
        if raw is None or not np.isfinite(float(raw)):
            raise ValueError(f"Disconnected/invalid PV: {name}")
        return float(raw)

    def read(self, pv):
        self.check_stop()
        # The setpoint defines the grid, the readback confirms the starting state.
        value, actual = self.scalar(pv), self.scalar(self.readbacks[pv])
        tolerance = .002 if pv in CURRENT_PVS else .1 if pv == PHASE_PV else .01
        if abs(value - actual) > tolerance:
            raise ValueError(f"Setpoint/readback mismatch: {pv}")
        return value

    def limits(self):
        limits = {TIMING_PV: (-float("inf"), float("inf")), PHASE_PV: (-float("inf"), float("inf"))}
        for pv in CURRENT_PVS:
            self.check_stop()
            ctrl = self.pv(pv).get_ctrlvars(timeout=1.) or {}
            lo, hi = float(ctrl.get("lower_ctrl_limit", np.nan)), float(ctrl.get("upper_ctrl_limit", np.nan))
            if not np.isfinite([lo, hi]).all() or lo >= hi:
                raise ValueError(f"Cannot verify corrector IOC limits: {pv}")
            limits[pv] = (max(-5., lo), min(5., hi))
        self.command_limits = limits
        return limits

    def write(self, values, restore=False):
        if not restore:
            self.check_stop()
        # Validate the whole vector before the first write.
        for pv, value in values.items():
            if pv not in PVS or not np.isfinite(value):
                raise ValueError(f"Invalid feedback command: {pv}")
            lo, hi = self.command_limits[pv]
            if not lo - 1e-9 <= value <= hi + 1e-9:
                raise ValueError(f"Feedback command outside limits: {pv}")
        journal_error = None
        try:
            self.emit(dict(kind="command", settings=values, restore=restore))
        except Exception as exc:
            if not restore:
                raise
            # A full disk must not prevent removal of a temporary probe.
            journal_error = exc
        put_errors = []
        for pv, value in values.items():
            if not restore:
                self.check_stop()
            try:
                result = self.pv(pv).put(float(value), wait=True, timeout=2.)
                if result != 1:
                    raise RuntimeError(f"PUT failed: {pv} (result={result})")
            except Exception as exc:
                if not restore:
                    raise
                put_errors.append(str(exc))
        if put_errors:
            raise RuntimeError("Restoration incomplete: " + "; ".join(put_errors))
        self.wait(self.config.settle_sec, restore=restore)
        deadline = time.monotonic() + 3.
        while True:
            if not restore:
                self.check_stop()
            matched = True
            for pv, target in values.items():
                tolerance = .002 if pv in CURRENT_PVS else .1 if pv == PHASE_PV else .01
                matched &= abs(self.scalar(self.readbacks[pv]) - target) <= tolerance
            if matched:
                if journal_error is not None:
                    raise RuntimeError("Restored settings, but command logging failed") from journal_error
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Feedback readback did not reach target")
            self.wait(.05, restore=restore)

    def metadata(self, name):
        self.check_stop()
        result = self.pv(name).get_with_metadata(use_monitor=False, form="time", timeout=1.)
        if not result or result.get("value") is None or not np.isfinite(result.get("timestamp", np.nan)):
            raise ValueError(f"No timestamped measurement: {name}")
        if result.get("severity", 0) >= 2:
            raise ValueError(f"Invalid alarm state: {name}")
        return result

    def fresh_records(self, names):
        # Establish a barrier AFTER settling. All three records must be newer.
        stamps = {name: self.metadata(name)["timestamp"] for name in names}
        samples = []
        deadline = time.monotonic() + self.config.sample_timeout_sec
        while len(samples) < 3:
            self.check_stop()
            records = {name: self.metadata(name) for name in names}
            if all(records[name]["timestamp"] > stamps[name] for name in names):
                stamps = {name: records[name]["timestamp"] for name in names}
                samples.append(records)
            elif time.monotonic() >= deadline:
                raise TimeoutError(f"Three fresh measurements unavailable: {', '.join(names)}")
            else:
                self.wait(.05)
        return samples

    def orbit(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS
        indices = [LINAC_MONITORS.index(name) for name in ("ML8L", "ML10L")]
        samples = []
        records = self.fresh_records(("LINAC:monitors",))
        for record in records:
            raw = np.asarray(record["LINAC:monitors"]["value"], float)
            if raw.size % 20 or raw.size < (max(indices) + 1) * 20:
                raise ValueError("Invalid LINAC:monitors waveform shape")
            selected = raw.reshape(-1, 20)[indices]
            if not np.all(selected[:, 0] == 1) or not np.isfinite(selected[:, 1:4]).all() or np.any(selected[:, 3] <= 0):
                raise ValueError("Invalid/no-beam ML8L or ML10L measurement")
            samples.append(np.r_[selected[:, 1], selected[:, 2]] / 1000.)
        self.emit(dict(kind="orbit_samples", samples_mm=np.asarray(samples).tolist(),
                       timestamps=[r["LINAC:monitors"]["timestamp"] for r in records]))
        return np.mean(samples, axis=0)

    def transmission(self):
        up, down = "BIM:LN0:nparticles", f"BIM:{self.config.downstream}:nparticles"
        records = self.fresh_records((up, down))
        values = np.array([[r[up]["value"], r[down]["value"]] for r in records], float)
        if values.shape != (3, 2) or not np.isfinite(values).all() or np.any(values[:, 0] <= 0) or np.any(values[:, 1] < 0):
            raise ValueError("Invalid/no-beam ICT measurement")
        result = float(values[:, 1].mean() / values[:, 0].mean())
        if not 0 <= result <= 1:
            raise ValueError("Transmission outside [0, 1]")
        self.emit(dict(kind="ict_samples", samples=values.tolist(),
                       timestamps=[[r[up]["timestamp"], r[down]["timestamp"]] for r in records]))
        return result

    def close(self):
        # These PVs have no callbacks/automatic subscriptions. Do not disconnect
        # shared CA channels that may also be used by the Main display.
        self.channels.clear()
