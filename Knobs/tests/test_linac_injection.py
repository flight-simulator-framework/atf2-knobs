"""Injected workers must never construct or fall back to hardware controls."""
import tempfile
import unittest
from unittest.mock import Mock, patch
from Knobs import Linac_Opt as opt

class InjectionTests(unittest.TestCase):
    def test_injected_worker_and_synchronous_write(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(opt, 'InterfaceATF2_LinacBT', side_effect=AssertionError('hardware')), patch.object(opt, 'DRBPMReader', side_effect=AssertionError('hardware')), patch('epics.PV', side_effect=AssertionError('hardware')):
            interface = Mock()
            worker = opt.OptimizationWorker({}, directory, interface=interface)
            self.assertIs(worker.interface, interface)
            worker._write_setpoints({'CM1L:phaseWrite': 1.}, wait=True)
            interface.write_setpoints_and_wait.assert_called_once_with({'CM1L:phaseWrite': 1.})

    def test_missing_synchronous_writer_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, patch('epics.PV', side_effect=AssertionError('hardware')):
            worker = opt.OptimizationWorker({}, directory, interface=object())
            with self.assertRaisesRegex(RuntimeError, 'hardware fallback is disabled'):
                worker._write_setpoints({'CM1L:phaseWrite': 1.}, wait=True)

if __name__ == '__main__':
    unittest.main()
