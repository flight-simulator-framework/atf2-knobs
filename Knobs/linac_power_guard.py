"""Read-only, shot-timestamped RF power interlock shared by both LINAC modes."""
import math
import threading
import time
from collections import deque

POWER_PVS = (("CM0L:poutReadPeak",)
             + tuple(f"CM{i}L:poutPeakRead" for i in range(1, 5))
             + tuple(f"CM{i}L:poutReadPeak" for i in range(5, 9)))


RECOVERY_SECONDS = 3.0


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
        self.healthy = set()
        self.healthy_since = None
        self.pending, self.events = deque(), deque()
        self.channels = []
        self.started = None
        self.closed = False

    def start(self):
        # EPICS timestamps are Unix time. Ignore cached pre-start samples.
        self.started = time.time()
        for pv in POWER_PVS:
            def connection(conn=None, _name=pv, **_):
                if not conn:
                    with self.lock:
                        self.healthy.discard(_name)
                        self.healthy_since = None
            channel = self.factory(pv, form="time", auto_monitor=True, connection_callback=connection)
            index = channel.add_callback(lambda pvname=None, _name=pv, **record:
                                         self.update(pvname=_name, **record), run_now=True)
            self.channels.append((channel, index, connection))

    def update(self, pvname=None, value=None, timestamp=None, severity=0, **kwargs):
        # CA callbacks only update protected state; journals/UI run on the worker.
        try:
            stamp = float(timestamp)
        except (TypeError, ValueError):
            return
        if pvname not in POWER_PVS or not math.isfinite(stamp):
            return
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = math.nan
        with self.lock:
            if self.closed or self.started is None or stamp <= max(self.started, self.stamps.get(pvname, -math.inf)):
                return
            self.stamps[pvname] = stamp
            valid = math.isfinite(value) and severity in (None, 0)
            if valid and value > .5 * self.baseline.get(pvname, 0.):
                self.healthy.add(pvname)
                if len(self.healthy) == len(POWER_PVS) and self.healthy_since is None:
                    self.healthy_since = time.monotonic()
            else:
                self.healthy.discard(pvname)
                self.healthy_since = None
            if not valid:
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

    def resume_if_recovered(self):
        """Acknowledge the trip atomically after all nine powers stay healthy."""
        with self.lock:
            if (self.closed or self.healthy_since is None
                    or time.monotonic() - self.healthy_since < RECOVERY_SECONDS):
                return False
            self.pending.clear()
            return True

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
        for channel, index, connection in self.channels:
            channel.remove_callback(index)
            callbacks = getattr(channel, "connection_callbacks", None)
            if isinstance(callbacks, list) and connection in callbacks:
                callbacks.remove(connection)
        self.channels.clear()
