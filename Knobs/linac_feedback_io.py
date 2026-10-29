"""EPICS acquisition for active feedback, owned exclusively by its worker.

Ten BPM or ICT records are required for each mean. Repeated cached
waveforms never count as separate samples. No diagnostic selectors are written.
"""
import time
import uuid
from collections import deque

import numpy as np

from Knobs.linac_feedback import BeamLoss, CURRENT_PVS, PHASE_PV, PVS, TIMING_PV
from Knobs.linac_power_guard import KlystronPowerGuard

BPM_SHOTS = 10
ICT_SHOTS = 10
ICT_LOSS_SHOTS = 20


class FeedbackMachine:
    def __init__(self, config, cancelled, checkpoint, emit, pv_factory=None):
        if pv_factory is None:
            from epics import PV, ca
            ca.use_initial_context()
            pv_factory = PV
        self.config, self.cancelled, self._checkpoint, self.emit = config, cancelled, checkpoint, emit
        self.pv_factory, self.channels = pv_factory, {}
        self.power_guard = None
        self.ict_pvs = ("BIM:LN0:nparticles", f"BIM:{config.downstream}:nparticles")
        self._last_beam_check = -float("inf")
        self._orbit_callback = None
        self._orbit_samples = deque(maxlen=BPM_SHOTS)
        self._ict_loss_counts = dict.fromkeys(self.ict_pvs, 0)
        self._ict_timestamps = dict.fromkeys(self.ict_pvs, -float("inf"))
        self._live_ict_records = {}
        self._live_ict_stamps = dict.fromkeys(self.ict_pvs, -float("inf"))
        self._live_ict_samples = deque(maxlen=ICT_SHOTS)
        self._bpm_invalid_count = 0
        self._bpm_check_timestamp = -float("inf")
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
        self.check_power()

    def start_power_monitor(self):
        self.power_guard = KlystronPowerGuard(self.pv_factory, self.emit)
        self.power_guard.start()

    def acknowledge_power_alarm(self):
        if self.power_guard is not None:
            self.power_guard.acknowledge()

    def check_power(self):
        if self.power_guard is None:
            return
        deadline = time.monotonic() + self.config.sample_timeout_sec
        while True:
            self.check_stop()
            self.power_guard.check()
            if self.power_guard.ready:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Waiting for first post-start RF power shot from Klystrons 0-8 timed out")
            time.sleep(.05)

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
        if abs(value - actual) > self.readback_tolerance(pv) + 1e-12:
            raise ValueError(f"Setpoint/readback mismatch: {pv}")
        return value

    @staticmethod
    def readback_tolerance(pv):
        # Active Feedback only: coarse hardware readbacks cannot resolve the
        # fine command grid. Confirmation still requires a successful PUT.
        return .1 if pv in CURRENT_PVS else 1. if pv == PHASE_PV else .01

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
                              self.readback_tolerance(pv) + 1e-12
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
        self.check_power()
        if force or time.monotonic() - self._last_beam_check >= .2:
            deadline = None
            while True:
                self.check_power()
                records = [self.metadata(name) for name in self.ict_pvs]
                if (not self.config.ict_auto_pause or all(float(record["value"]) > 0 for record in records)) and self.poll_orbit() is not False:
                    break
                # Do not issue a new command or consume no-beam BPM data while
                # a drop is unconfirmed. Wait for recovery or twenty new losses.
                if deadline is None:
                    deadline = time.monotonic() + self.config.sample_timeout_sec
                if time.monotonic() >= deadline:
                    raise TimeoutError("Fresh monitor measurements unavailable while confirming invalid data")
                self.check_stop()
                time.sleep(.05)
            self._last_beam_check = time.monotonic()

    def set_orbit_monitor(self, callback):
        self._orbit_callback = callback
        self._orbit_samples.clear()
        self._orbit_timestamp = None
        self._orbit_last_seen = time.monotonic()

    def reset_monitor_batches(self):
        """Keep calibration shots out of feedback's first monitor batches."""
        self._orbit_samples.clear()
        self._live_ict_samples.clear()
        self._live_ict_records.clear()

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
        if not record["feedback_valid"]:
            return False
        stamp = record["timestamp"]
        if self._orbit_timestamp is None or stamp > self._orbit_timestamp:
            self._orbit_samples.append(self.decode_orbit(record))
            self._orbit_timestamp = stamp
            self._orbit_last_seen = time.monotonic()
            if len(self._orbit_samples) == BPM_SHOTS:
                orbit = np.mean(self._orbit_samples, axis=0)
                self._orbit_samples.clear()
                self.emit(dict(kind="orbit_monitor", orbit_mm=orbit.tolist(), ioc_timestamp=stamp, shots=BPM_SHOTS))
                self._orbit_callback(orbit)
        elif time.monotonic() - self._orbit_last_seen > self.config.sample_timeout_sec:
            raise TimeoutError("LINAC:monitors stopped updating")

    def check_bpm(self, record):
        try:
            self.decode_orbit(record)
            if record.get("severity", 0) >= 2:
                raise ValueError("Invalid alarm state: LINAC:monitors")
            valid, reason = True, ""
        except ValueError as exc:
            valid, reason = False, str(exc)
        if record["timestamp"] > self._bpm_check_timestamp:
            self._bpm_check_timestamp = record["timestamp"]
            self._bpm_invalid_count = 0 if valid else self._bpm_invalid_count + 1
            if not valid:
                self._orbit_samples.clear()
                self.emit(dict(kind="bpm_invalid", consecutive=self._bpm_invalid_count,
                               ioc_timestamp=record["timestamp"], reason=reason))
        return valid

    def check_ict(self, name, value, timestamp):
        if name not in self.ict_pvs:
            return
        if not np.isfinite(float(value)):
            raise ValueError(f"Invalid ICT measurement: {name}")
        if not self.config.ict_auto_pause:
            return
        if timestamp > self._ict_timestamps[name]:
            self._ict_timestamps[name] = timestamp
            self._ict_loss_counts[name] = self._ict_loss_counts[name] + 1 if float(value) <= 0 else 0
            if self._ict_loss_counts[name]:
                self.emit(dict(kind="beam_loss_detected" if self._ict_loss_counts[name] >= ICT_LOSS_SHOTS else "beam_loss_pending",
                               pv=name, value=float(value), ioc_timestamp=timestamp,
                               consecutive=self._ict_loss_counts[name], required=ICT_LOSS_SHOTS))
        if self._ict_loss_counts[name] >= ICT_LOSS_SHOTS:
            raise BeamLoss(f"{name}: ICT ≤ 0 for {ICT_LOSS_SHOTS} consecutive shots")

    def metadata(self, name):
        self.check_stop()
        result = self.pv(name).get_with_metadata(use_monitor=False, form="time", timeout=1.)
        if not result or result.get("value") is None or not np.isfinite(result.get("timestamp", np.nan)):
            raise ValueError(f"No timestamped measurement: {name}")
        self.check_ict(name, result["value"], result["timestamp"])
        if name == "LINAC:monitors":
            return dict(result, feedback_valid=self.check_bpm(result))
        # A no-beam alarm on an ICT must follow the same consecutive-shot decision.
        if result.get("severity", 0) >= 2 and not (name in self.ict_pvs and float(result["value"]) <= 0):
            raise ValueError(f"Invalid alarm state: {name}")
        if name in self.ict_pvs:
            self.monitor_transmission(name, result)
        return result

    def monitor_transmission(self, name, record):
        """Display-only nonoverlapping ten-shot means during scans and waits.

Both ICT timestamps must advance for a new pair. Control measurements still
use fresh_records after settling and never consume this monitor batch.
"""
        previous = self._live_ict_records.get(name)
        if previous is not None and record["timestamp"] <= previous["timestamp"]:
            return
        self._live_ict_records[name] = record
        if self.invalid_ict_sample(name, float(record["value"])):
            self._live_ict_samples.clear()
            # Do not pair a recovered channel with a pre-loss sample.
            self._live_ict_stamps.update({pv: value["timestamp"] for pv, value in self._live_ict_records.items()})
            return
        if not all(pv in self._live_ict_records and
                   self._live_ict_records[pv]["timestamp"] > self._live_ict_stamps[pv] and
                   not self.invalid_ict_sample(pv, float(self._live_ict_records[pv]["value"])) for pv in self.ict_pvs):
            return
        pair = [self._live_ict_records[pv] for pv in self.ict_pvs]
        self._live_ict_stamps = {pv: item["timestamp"] for pv, item in zip(self.ict_pvs, pair)}
        self._live_ict_samples.append([float(item["value"]) for item in pair])
        if len(self._live_ict_samples) == ICT_SHOTS:
            values = np.asarray(self._live_ict_samples)
            self._live_ict_samples.clear()
            transmission = float(values[:, 1].mean() / values[:, 0].mean())
            if 0 <= transmission <= 1:
                self.emit(dict(kind="transmission_monitor", transmission=transmission, shots=ICT_SHOTS,
                               ioc_timestamps=dict(self._live_ict_stamps)))

    def invalid_ict_sample(self, name, value):
        # A zero downstream measurement is valid zero transmission while the
        # ICT interlock is disabled; a zero denominator cannot define a ratio.
        return value <= 0 if self.config.ict_auto_pause or name == self.ict_pvs[0] else value < 0

    def fresh_records(self, names):
        # Establish a barrier AFTER settling. Every record must be newer.
        count = BPM_SHOTS if "LINAC:monitors" in names else ICT_SHOTS
        self.check_beam(force=True)
        stamps = {name: self.metadata(name)["timestamp"] for name in names}
        samples = []
        deadline = time.monotonic() + self.config.sample_timeout_sec
        last_update = -float("inf")
        detail = "Reading BPM orbit" if "LINAC:monitors" in names else "Reading transmission"
        while len(samples) < count:
            self.check_stop()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{count} fresh measurements unavailable: {', '.join(names)}")
            self.check_beam()
            records = {name: self.metadata(name) for name in names}
            if all(records[name]["timestamp"] > stamps[name] for name in names):
                stamps = {name: records[name]["timestamp"] for name in names}
                if any(not records[name].get("feedback_valid", True) or
                       (name in self.ict_pvs and self.invalid_ict_sample(name, float(records[name]["value"]))) for name in names):
                    samples.clear()
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{count} valid fresh measurements unavailable: {', '.join(names)}")
                    self.check_beam(force=True)
                else:
                    samples.append(records)
                last_update = -float("inf")
            elif time.monotonic() >= deadline:
                raise TimeoutError(f"{count} fresh measurements unavailable: {', '.join(names)}")
            else:
                self.wait(.05)
            if time.monotonic() - last_update >= .5:
                self.emit(dict(kind="activity", detail=f"{detail}: {len(samples)} / {count} fresh samples"))
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
        if values.shape != (ICT_SHOTS, 2) or not np.isfinite(values).all():
            raise ValueError("Invalid ICT measurement")
        if np.any(values[:, 0] <= 0) or np.any(values[:, 1] <= 0 if self.config.ict_auto_pause else values[:, 1] < 0):
            raise ValueError("Nonpositive ICT sample in transmission average")
        result = float(values[:, 1].mean() / values[:, 0].mean())
        if not 0 <= result <= 1:
            raise ValueError("Transmission outside [0, 1]")
        self.emit(dict(kind="ict_samples", samples=values.tolist(),
                       timestamps=[[r[up]["timestamp"], r[down]["timestamp"]] for r in records]))
        return result

    def close(self):
        if self.power_guard is not None:
            self.power_guard.close()
        # These PVs have no callbacks/automatic subscriptions. Do not disconnect
        # shared CA channels that may also be used by the Main display.
        self.channels.clear()
