"""Display-only CA subscriptions; snapshots never issue GET or PUT requests."""
import math
import threading
from Knobs.timing_pv import validate_timing_pvs


class CurrentValueMonitor:
    def __init__(self, specs, pv_factory=None):
        if pv_factory is None:
            from epics import PV
            pv_factory = PV
        self._lock = threading.Lock()
        self._closed = False
        self._values = {}
        self._pvs = []
        self._sources = {write: read or write
                         for group in specs.values() for _, write, read, _ in group}
        validate_timing_pvs(list(self._sources) + list(self._sources.values()))
        try:
            for name in dict.fromkeys(self._sources.values()):
                self._values[name] = math.nan
                pv = pv_factory(name, auto_monitor=True, form="time", count=1,
                                callback=self._on_value,
                                connection_callback=self._on_connection)
                self._pvs.append(pv)
        except Exception:
            self.close()
            raise

    def _on_connection(self, pvname=None, conn=None, **kwargs):
        # Wait for a fresh initial notification after either connection change.
        with self._lock:
            if not self._closed:
                self._values[pvname] = math.nan

    def _on_value(self, pvname=None, value=None, **kwargs):
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            value = math.nan
        with self._lock:
            if not self._closed:
                self._values[pvname] = value

    def snapshot(self):
        with self._lock:
            values = {write: self._values.get(source, math.nan)
                      for write, source in self._sources.items()}
        # Match the existing phase display precision without machine I/O.
        return {write: round(value / .5) * .5
                if math.isfinite(value) and "phase" in self._sources[write].lower()
                else value for write, value in values.items()}

    def close(self):
        with self._lock:
            self._closed = True
            self._values.clear()
        pvs, self._pvs = self._pvs, []
        for pv in pvs:
            # Remove this object's subscription/callbacks. Do not clear the
            # underlying shared CA channel used by optimizer PV objects.
            pv.disconnect()
