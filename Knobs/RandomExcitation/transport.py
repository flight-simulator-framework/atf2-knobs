"""Persistent CA monitors and an isolated, explicitly selected demo transport."""
from collections import deque
from dataclasses import dataclass
import math
import threading
import time

import numpy as np

from .catalog import CATALOG, DIAGNOSTIC_PVS, DR_MONITORS, DR_MODE, DR_TURN, ICT_PVS, XSR_PVS


@dataclass(frozen=True)
class Sample:
    value: object
    timestamp: float
    received: float
    sequence: int
    connected: bool = True
    severity: int = 0
    alarm_status: int = 0

    def scalar(self):
        try:
            v = np.asarray(self.value)
            return float(v.flat[0]) if v.size == 1 and self.connected and self.severity < 2 else math.nan
        except (TypeError, ValueError):
            return math.nan


class MonitorCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._samples = {}
        self._history = {}
        self._closed = False
        self._sequence = 0

    def on_value(self, pvname=None, value=None, timestamp=None, severity=0, status=0, **kwargs):
        with self._lock:
            if self._closed:
                return
            self._sequence += 1
            if isinstance(value, np.ndarray):
                value = value.copy()
            sample = Sample(value, float(timestamp or 0), time.monotonic(), self._sequence,
                            True, int(severity or 0), int(status or 0))
            self._samples[pvname] = sample
            history = self._history.setdefault(pvname, deque(maxlen=512 if pvname in ICT_PVS.values() else 2))
            history.append(sample)

    def on_connection(self, pvname=None, conn=None, **kwargs):
        if not conn:
            with self._lock:
                self._sequence += 1
                self._samples[pvname] = Sample(math.nan, 0, time.monotonic(), self._sequence, False)
                self._history.pop(pvname, None)

    def sample(self, name):
        with self._lock:
            return self._samples.get(name, Sample(math.nan, 0, 0, 0, False))

    def snapshot(self):
        with self._lock:
            return dict(self._samples)

    def history(self, name, after_sequence=0):
        with self._lock:
            return [s for s in self._history.get(name, ()) if s.sequence > after_sequence]

    def wait(self, seconds):
        time.sleep(max(0, seconds))


class EpicsTransport(MonitorCache):
    is_demo = False

    def __init__(self, pv_factory=None):
        super().__init__()
        if pv_factory is None:
            from epics import PV, ca
            ca.use_initial_context()
            pv_factory = PV
        self._factory = pv_factory
        self._pvs = {}
        self._control_metadata = {}
        self._writable = set()

    def subscribe(self, names):
        # Create all channels before waiting: a missing optional DR control must
        # not cause one connection timeout per channel.
        for name in dict.fromkeys(names):
            if name not in self._pvs:
                pv = self._factory(name, form="time", auto_monitor=True,
                                   callback=self.on_value, connection_callback=self.on_connection)
                self._pvs[name] = pv

    def connect(self, timeout=4.0):
        names = list(DIAGNOSTIC_PVS)
        for a in CATALOG:
            names.extend(a.sources)
        self.subscribe(names)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(self.sample(name).connected for name in names):
                break
            self.wait(0.03)
        # Metadata reads occur only on explicit Connect/refresh, not in the
        # live display or acquisition loop.
        for a in CATALOG:
            for name in a.writes:
                pv = self._pvs[name]
                if pv.connected:
                    if pv.write_access:
                        self._writable.add(name)
                    self._control_metadata[name] = pv.get_ctrlvars(timeout=0.3) or {}
        return self

    def limits(self, name):
        return dict(self._control_metadata.get(name, {}))

    def can_write(self, name):
        if not any(name in a.writes for a in CATALOG):
            return False
        pv = self._pvs.get(name)
        return bool(pv is not None and pv.connected and pv.write_access)

    def put(self, name, value, timeout=2.0):
        if name in DIAGNOSTIC_PVS or not self.can_write(name):
            raise RuntimeError(f"PV is not an available actuator: {name}")
        result = self._pvs[name].put(float(value), wait=True, timeout=timeout)
        if result != 1:
            raise RuntimeError(f"PV put completion failed: {name} (status={result})")

    def close(self):
        with self._lock:
            self._closed = True
        pvs, self._pvs = self._pvs, {}
        for pv in pvs.values():
            pv.disconnect()


class DemoTransport(MonitorCache):
    """Synthetic signals, no epics import, network access or hardware fallback."""
    is_demo = True

    def __init__(self, period=1 / 3, seed=42):
        super().__init__()
        self.period = period
        self.rng = np.random.default_rng(seed)
        self.commands = []
        self._metadata = {}
        self._stop = threading.Event()
        self._thread = None

    def publish(self, name, value):
        self.on_value(pvname=name, value=value, timestamp=time.time())

    def connect(self, timeout=0):
        for a in CATALOG:
            origin = 100. if a.name == "SOLENOIDE" else (3. if a.group in ("qa", "qm") else 0.)
            if a.kind == "timing":
                self.publish(a.writes[0], 1120.)
            else:
                self.publish(a.writes[0], origin)
                if a.read:
                    self.publish(a.read, origin)
            if a.kind == "current":
                bounds = (80., 125.) if a.name == "SOLENOIDE" else ((0., 10.) if a.group in ("qa", "qm") else (-5., 5.))
                for name, value in zip(a.limit_pvs, bounds):
                    self.publish(name, value)
                self._metadata[a.writes[0]] = dict(lower_ctrl_limit=bounds[0], upper_ctrl_limit=bounds[1])
        self.publish(DR_MODE, 1)
        self.publish(DR_TURN, 1)
        self._tick()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(self.period):
            self._tick()

    def _tick(self):
        phases = [self.sample(a.writes[0]).scalar() for a in CATALOG if a.kind == "phase"]
        transmission = 0.9 * math.exp(-sum((x / 150) ** 2 for x in phases))
        gun = max(0., 4. + self.rng.normal(0, .03))
        for i, name in enumerate(ICT_PVS.values()):
            self.publish(name, gun * transmission ** (i / 5) + self.rng.normal(0, .015))
        waveform = np.zeros((98, 10))
        waveform[:, 0] = 1
        waveform[:, 1:3] = self.rng.normal(0, 20, (98, 2))
        waveform[:, 3] = gun * transmission
        self.publish(DR_MONITORS, waveform.ravel())
        for key, name in XSR_PVS.items():
            self.publish(name, (15. if "SIGMA" in key else 0.) + self.rng.normal(0, .1))

    def limits(self, name):
        return dict(self._metadata.get(name, {}))

    def can_write(self, name):
        return any(name in a.writes for a in CATALOG)

    def put(self, name, value, timeout=2):
        if not self.can_write(name):
            raise RuntimeError(f"Diagnostic PV cannot be written: {name}")
        self.commands.append((name, value))
        self.publish(name, value)
        for a in CATALOG:
            if name in a.writes and a.read:
                self.publish(a.read, value)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._closed = True
