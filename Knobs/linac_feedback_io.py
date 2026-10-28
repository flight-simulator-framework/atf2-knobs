"""EPICS acquisition for active feedback, owned exclusively by its worker.

Three new timestamped records are required for each mean. Repeated cached
waveforms never count as separate samples. No diagnostic selectors are written.
"""
import time
import uuid
from collections import deque

import numpy as np

from Knobs.linac_feedback import BeamLoss, CURRENT_PVS, PHASE_PV, PVS, TIMING_PV


class FeedbackMachine:
    def __init__(self, config, cancelled, checkpoint, emit, pv_factory=None):
        if pv_factory is None:
            from epics import PV, ca
            ca.use_initial_context()
            pv_factory = PV
        self.config, self.cancelled, self._checkpoint, self.emit = config, cancelled, checkpoint, emit
        self.pv_factory, self.channels = pv_factory, {}
        self.ict_pvs = ("BIM:LN0:nparticles", f"BIM:{config.downstream}:nparticles")
        self._last_beam_check = -float("inf")
        self._orbit_callback = None
        self._orbit_samples = deque(maxlen=3)
        self._orbit_timestamp = None
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

    def wait(self, seconds, restore=False, activity=None):
        deadline = time.monotonic() + seconds
        last_update = -float("inf")
        while time.monotonic() < deadline:
            if not restore:
                self.check_stop()
                self.check_beam()
            if activity and time.monotonic() - last_update >= .5:
                self.emit(dict(kind="activity", detail=activity, remaining_sec=max(0., deadline - time.monotonic())))
                last_update = time.monotonic()
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
            self.check_beam(force=True)
        # Validate the whole vector before the first write.
        for pv, value in values.items():
            if pv not in PVS or not np.isfinite(value):
                raise ValueError(f"Invalid feedback command: {pv}")
            lo, hi = self.command_limits[pv]
            if not lo - 1e-9 <= value <= hi + 1e-9:
                raise ValueError(f"Feedback command outside limits: {pv}")
        previous = {}
        for pv in values:
            try:
                previous[pv] = self.scalar(pv)
            except Exception:
                if not restore:
                    raise
                previous[pv] = None  # A missing pre-read must not prevent undo.
        command = dict(command_id=uuid.uuid4().hex, settings=dict(values), previous=previous, restore=restore)
        journal_errors = []

        def record(kind, **details):
            try:
                self.emit(dict(command, kind=kind, **details))
            except Exception as exc:
                if not restore:
                    raise
                journal_errors.append(exc)

        record("command", status="requested")
        written, readbacks = [], {}
        try:
            put_errors = []
            for pv, value in values.items():
                if not restore:
                    self.check_stop()
                try:
                    result = self.pv(pv).put(float(value), wait=True, timeout=2.)
                    if result != 1:
                        raise RuntimeError(f"PUT failed: {pv} (result={result})")
                    written.append(pv)
                except Exception as exc:
                    if not restore:
                        raise
                    put_errors.append(str(exc))
            if put_errors:
                raise RuntimeError("Restoration incomplete: " + "; ".join(put_errors))
            self.wait(self.config.settle_sec, restore=restore,
                      activity="Restoring accepted settings" if restore else "Settling after write")
            deadline = time.monotonic() + 3.
            while True:
                if not restore:
                    self.check_stop()
                    self.check_beam()
                readbacks = {pv: self.scalar(self.readbacks[pv]) for pv in values}
                matched = all(abs(readbacks[pv] - target) <=
                              (.002 if pv in CURRENT_PVS else .1 if pv == PHASE_PV else .01)
                              for pv, target in values.items())
                if matched:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Feedback readback did not reach target")
                self.wait(.05, restore=restore)
            record("command_result", status="confirmed", readbacks=readbacks, written=written)
        except Exception as exc:
            # Preserve the original failure even if the audit file also fails.
            try:
                record("command_result", status="failed", error=str(exc), readbacks=readbacks, written=written)
            except Exception:
                pass
            raise
        if journal_errors:
            raise RuntimeError("Restored settings, but command logging failed") from journal_errors[0]

    def check_beam(self, force=False):
        """Monitor selected upstream/downstream ICTs even during orbit work/waits."""
        if force or time.monotonic() - self._last_beam_check >= .2:
            for name in self.ict_pvs:
                self.metadata(name)
            self.poll_orbit()
            self._last_beam_check = time.monotonic()

    def set_orbit_monitor(self, callback):
        self._orbit_callback = callback
        self._orbit_samples.clear()
        self._orbit_timestamp = None
        self._orbit_last_seen = time.monotonic()

    @staticmethod
    def decode_orbit(record):
        from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS
        indices = [LINAC_MONITORS.index(name) for name in ("ML8L", "ML10L")]
        raw = np.asarray(record["value"], float)
        if raw.size % 20 or raw.size < (max(indices) + 1) * 20:
            raise ValueError("Invalid LINAC:monitors waveform shape")
        selected = raw.reshape(-1, 20)[indices]
        if not np.all(selected[:, 0] == 1) or not np.isfinite(selected[:, 1:4]).all() or np.any(selected[:, 3] <= 0):
            raise ValueError("Invalid/no-beam ML8L or ML10L measurement")
        return np.r_[selected[:, 1], selected[:, 2]] / 1000.

    def poll_orbit(self):
        if self._orbit_callback is None:
            return
        record = self.metadata("LINAC:monitors")
        stamp = record["timestamp"]
        if self._orbit_timestamp is None or stamp > self._orbit_timestamp:
            self._orbit_samples.append(self.decode_orbit(record))
            self._orbit_timestamp = stamp
            self._orbit_last_seen = time.monotonic()
            if len(self._orbit_samples) == 3:
                orbit = np.mean(self._orbit_samples, axis=0)
                self.emit(dict(kind="orbit_monitor", orbit_mm=orbit.tolist(), ioc_timestamp=stamp))
                self._orbit_callback(orbit)
        elif time.monotonic() - self._orbit_last_seen > self.config.sample_timeout_sec:
            raise TimeoutError("LINAC:monitors stopped updating")

    def check_ict(self, name, value, timestamp=None):
        if name not in self.ict_pvs:
            return
        if not np.isfinite(float(value)):
            raise ValueError(f"Invalid ICT measurement: {name}")
        if float(value) <= 0:
            self.emit(dict(kind="beam_loss_detected", pv=name, value=float(value), ioc_timestamp=timestamp))
            raise BeamLoss(f"{name} = {float(value):g} (ICT ≤ 0)")

    def metadata(self, name):
        self.check_stop()
        result = self.pv(name).get_with_metadata(use_monitor=False, form="time", timeout=1.)
        if not result or result.get("value") is None or not np.isfinite(result.get("timestamp", np.nan)):
            raise ValueError(f"No timestamped measurement: {name}")
        self.check_ict(name, result["value"], result["timestamp"])
        if result.get("severity", 0) >= 2:
            raise ValueError(f"Invalid alarm state: {name}")
        return result

    def fresh_records(self, names):
        # Establish a barrier AFTER settling. All three records must be newer.
        self.check_beam(force=True)
        stamps = {name: self.metadata(name)["timestamp"] for name in names}
        samples = []
        deadline = time.monotonic() + self.config.sample_timeout_sec
        last_update = -float("inf")
        detail = "Reading BPM orbit" if "LINAC:monitors" in names else "Reading transmission"
        while len(samples) < 3:
            self.check_stop()
            self.check_beam()
            records = {name: self.metadata(name) for name in names}
            if all(records[name]["timestamp"] > stamps[name] for name in names):
                stamps = {name: records[name]["timestamp"] for name in names}
                samples.append(records)
                last_update = -float("inf")
            elif time.monotonic() >= deadline:
                raise TimeoutError(f"Three fresh measurements unavailable: {', '.join(names)}")
            else:
                self.wait(.05)
            if time.monotonic() - last_update >= .5:
                self.emit(dict(kind="activity", detail=f"{detail}: {len(samples)} / 3 fresh samples"))
                last_update = time.monotonic()
        return samples

    def orbit(self):
        records = self.fresh_records(("LINAC:monitors",))
        samples = [self.decode_orbit(record["LINAC:monitors"]) for record in records]
        self.emit(dict(kind="orbit_samples", samples_mm=np.asarray(samples).tolist(),
                       timestamps=[r["LINAC:monitors"]["timestamp"] for r in records]))
        return np.mean(samples, axis=0)

    def transmission(self):
        up, down = "BIM:LN0:nparticles", f"BIM:{self.config.downstream}:nparticles"
        records = self.fresh_records((up, down))
        values = np.array([[r[up]["value"], r[down]["value"]] for r in records], float)
        if values.shape != (3, 2) or not np.isfinite(values).all():
            raise ValueError("Invalid ICT measurement")
        for row in values:
            for name, value in zip((up, down), row):
                self.check_ict(name, value)
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
