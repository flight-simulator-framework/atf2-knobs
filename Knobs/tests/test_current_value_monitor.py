"""No real CA connections: verify subscription-only display and cleanup."""
import math
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from unittest.mock import Mock, patch
from Knobs.current_value_monitor import CurrentValueMonitor


class FakePV:
    def __init__(self, name, **kwargs):
        self.name = name
        self.options = kwargs
        self.disconnected = 0

    def get(self, *args, **kwargs):
        raise AssertionError("display must never GET")

    put = get

    def disconnect(self):
        self.disconnected += 1

    def update(self, value):
        self.options["callback"](pvname=self.name, value=value)

    def connection(self, connected):
        self.options["connection_callback"](pvname=self.name, conn=connected)


class MonitorTests(unittest.TestCase):
    specs = {"a": [("A", "A:set", "A:phaseRead", "deg"),
                   ("B", "B:set", None, "A")],
             "duplicate": [("A", "A:set", "A:phaseRead", "deg")]}

    def make_monitor(self, specs=None):
        pvs = []
        def factory(name, **kwargs):
            pv = FakePV(name, **kwargs)
            pvs.append(pv)
            return pv
        return CurrentValueMonitor(specs or self.specs, factory), pvs

    def test_subscribe_once_snapshot_without_get_and_disconnect(self):
        monitor, pvs = self.make_monitor()
        self.assertEqual([pv.name for pv in pvs], ["A:phaseRead", "B:set"])
        self.assertTrue(all(pv.options["auto_monitor"] for pv in pvs))
        self.assertTrue(math.isnan(monitor.snapshot()["A:set"]))
        pvs[0].update(1.23)
        pvs[1].update(7.3)
        for _ in range(100):
            self.assertEqual(monitor.snapshot(), {"A:set": 1., "B:set": 7.3})
        pvs[0].connection(False)
        self.assertTrue(math.isnan(monitor.snapshot()["A:set"]))
        pvs[0].connection(True)
        self.assertTrue(math.isnan(monitor.snapshot()["A:set"]))
        pvs[0].update(2.6)
        self.assertEqual(monitor.snapshot()["A:set"], 2.5)
        pvs[1].update(None)
        self.assertTrue(math.isnan(monitor.snapshot()["B:set"]))
        monitor.close()
        monitor.close()
        pvs[0].update(99)
        self.assertTrue(math.isnan(monitor.snapshot()["A:set"]))
        self.assertEqual([pv.disconnected for pv in pvs], [1, 1])

    def test_partial_start_failure_releases_created_subscription(self):
        first = FakePV("A", auto_monitor=True)
        with self.assertRaisesRegex(RuntimeError, "failed"):
            CurrentValueMonitor(self.specs, Mock(side_effect=[first, RuntimeError("failed")]))
        self.assertEqual(first.disconnected, 1)

    def test_old_timing_specs_fail_before_any_channel_is_created(self):
        for name in ("EVE_LINAC.OT8D", "EVE_LINAC.RFDLY0"):
            for write, read in ((name, None), ("EVE_LINAC:OUT0:SetData", name)):
                factory = Mock()
                with self.assertRaisesRegex(ValueError, "Timing PV access disabled"):
                    CurrentValueMonitor({"timing": [("Timing", write, read, "ns")]}, factory)
                factory.assert_not_called()

    def test_main_refresh_reuses_monitor_and_never_constructs_interface(self):
        from Knobs import Linac_Opt as opt
        window = Mock()
        window._current_value_monitor = None
        monitor, pvs = self.make_monitor(opt.TARGET_VALUE_SPECS)
        self.assertEqual(len(pvs), 20)
        self.assertEqual([p.name for p in pvs if p.name.startswith("EVE_LINAC")], ["EVE_LINAC:OUT0:SetData"])
        with patch.object(opt, "CurrentValueMonitor", return_value=monitor) as factory, \
             patch.object(opt, "InterfaceATF2_LinacBT", side_effect=AssertionError("hardware")):
            for _ in range(3):
                opt.MainWindow._refresh_current_values(window)
            factory.assert_called_once_with(opt.TARGET_VALUE_SPECS)
            window._ensure_interface.assert_not_called()
            self.assertEqual(window._update_value_labels.call_count, 3)
        monitor.close()


if __name__ == "__main__":
    unittest.main()
