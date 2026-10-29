"""Read-only, shot-timestamped RF power interlock shared by both LINAC modes."""
import math
import threading
import time
from collections import deque

POWER_PVS = tuple(f"CM{i}L:poutPeakRead" for i in range(9))


class KlystronPowerLoss(RuntimeError):
    pass


class KlystronPowerGuard:
    def __init__(self, pv_factory=None, emit=lambda event: None):
        if pv_factory is None:
            from epics import PV, ca
            ca.use_initial_context()
            pv_factory = PV
        self.factory, self.emit = pv_factory, emit
        self.lock = threading.Lock()
        self.baseline, self.latest, self.stamps = {}, {}, {}
        self.pending, self.events = deque(), deque()
        self.channels = []
        self.started = None
        self.closed = False

    def start(self):
        # EPICS timestamps are Unix time. Ignore cached pre-start samples.
        self.started = time.time()
        for pv in POWER_PVS:
            channel = self.factory(pv, form="time", auto_monitor=True)
            index = channel.add_callback(lambda pvname=None, _name=pv, **record:
                                         self.update(pvname=_name, **record), run_now=True)
            self.channels.append((channel, index))

    def update(self, pvname=None, value=None, timestamp=None, severity=0, **kwargs):
        # CA callbacks only update protected state; journals/UI run on the worker.
        try:
            value, stamp = float(value), float(timestamp)
        except (TypeError, ValueError):
            return
        if pvname not in POWER_PVS or not math.isfinite(stamp):
            return
        with self.lock:
            if self.closed or self.started is None or stamp <= max(self.started, self.stamps.get(pvname, -math.inf)):
                return
            self.stamps[pvname] = stamp
            if not math.isfinite(value) or severity not in (None, 0):
                return  # Never turn a failed read into a zero-power observation.
            self.latest[pvname] = value
            if pvname not in self.baseline:
                if value <= 0:
                    reason = f"Klystron {POWER_PVS.index(pvname)} has no RF power; cannot establish its start baseline."
                    if not self.pending:
                        self.pending.append(reason)
                    return
                self.baseline[pvname] = value
                self.events.append(dict(kind="klystron_power_reference", pv=pvname, value=value,
                                        threshold=value * .5, ioc_timestamp=stamp))
            elif value <= .5 * self.baseline[pvname]:
                reason = (f"Klystron {POWER_PVS.index(pvname)} RF power is too low: "
                          f"{value:g} <= 50% of start power ({self.baseline[pvname]:g}).")
                # Latch even a single low shot that recovers before the next check.
                if not self.pending:
                    self.pending.append(reason)
                    self.events.append(dict(kind="klystron_power_low", pv=pvname, value=value,
                                            baseline=self.baseline[pvname], threshold=.5 * self.baseline[pvname],
                                            ioc_timestamp=stamp, message=reason))

    def check(self):
        with self.lock:
            events = list(self.events)
            self.events.clear()
            reason = self.pending.popleft() if self.pending else None
            if reason is None:
                for pv, baseline in self.baseline.items():
                    if self.latest[pv] <= baseline * .5:
                        reason = f"Klystron {POWER_PVS.index(pv)} RF power is still at or below 50% of start power."
                        break
        for event in events:
            self.emit(event)
        if reason:
            raise KlystronPowerLoss(reason)

    @property
    def ready(self):
        with self.lock:
            return len(self.baseline) == len(POWER_PVS)

    def acknowledge(self):
        """Manual Resume acknowledges shots already covered by the current pause.

        Keep the interlock latched if any current power is still too low.
        New low callbacks after this acknowledgement latch a new fault.
        """
        with self.lock:
            if len(self.baseline) == len(POWER_PVS) and all(
                    self.latest[pv] > baseline * .5 for pv, baseline in self.baseline.items()):
                self.pending.clear()

    def close(self):
        with self.lock:
            self.closed = True
        for channel, index in self.channels:
            channel.remove_callback(index)
        self.channels.clear()
