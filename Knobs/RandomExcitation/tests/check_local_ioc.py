"""Exercise the production PyEPICS transport against a disposable loopback IOC.

Requires caproto, pyepics and libca. Never searches the accelerator network.
Run this as a separate process, before any other CA context is initialized.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    port, repeater = free_port(), free_port()
    os.environ.update(EPICS_CA_AUTO_ADDR_LIST='NO', EPICS_CA_ADDR_LIST=f'127.0.0.1:{port}',
                      EPICS_CA_SERVER_PORT=str(port), EPICS_CAS_SERVER_PORT=str(port),
                      EPICS_CA_REPEATER_PORT=str(repeater), EPICS_CAS_INTF_ADDR_LIST='127.0.0.1',
                      EPICS_CAS_AUTO_BEACON_ADDR_LIST='NO', EPICS_CAS_BEACON_ADDR_LIST='127.0.0.1')
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    with tempfile.TemporaryDirectory(prefix='random-excitation-ioc-') as folder:
        with open(Path(folder)/'ioc.log', 'w+') as log:
            server = subprocess.Popen([sys.executable, str(Path(__file__).with_name('local_ioc.py'))],
                                      env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
            backend = None
            try:
                time.sleep(.7)
                if server.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read())
                from epics import PV
                from Knobs.RandomExcitation.transport import EpicsTransport
                from Knobs.RandomExcitation.catalog import BY_NAME, DR_MODE, DR_TURN
                from Knobs.RandomExcitation.core import Config, Runner, prepare

                def pv_factory(name, **kwargs):
                    for key in ('callback', 'connection_callback'):
                        original = kwargs[key]
                        def wrapped(*args, cb=original, **data):
                            data['pvname'] = data['pvname'].removeprefix('BDXRTEST:')
                            cb(*args, **data)
                        kwargs[key] = wrapped
                    return PV('BDXRTEST:' + name, **kwargs)

                backend = EpicsTransport(pv_factory=pv_factory).connect(timeout=3)
                config = Config(max_points=12, samples=3, guard_seconds=.05, arrival_timeout=3,
                                measurement_timeout=2, end_action='initial', reference_every=0,
                                expansion_points=2, expansion_coverage=1)
                p = prepare(BY_NAME['QA1L'], backend, config)
                assert (p.lower, p.upper) == (1.2, 4.8), p
                assert 'parameter.A' in p.limit_source
                assert 'INVALID' not in p.limit_source, p.limit_source
                assert backend.sample('QA1L:parameter.A').alarm_status == 17
                assert backend.sample('QA1L:currentWrite').severity == 3
                assert p.origin == 3
                events = []
                runner = Runner(backend, [BY_NAME['L1'], BY_NAME['QA1L']], config, folder, emit=events.append)
                result = runner.run()
                assert result == 'complete', events[-1]
                points = [e for e in events if e['state'] == 'point']
                assert len(points) == 12 and all(p['quality'] == 'complete' for p in points)
                assert runner.progressive.stage > 0
                assert any(e['state'] == 'exploration_range' and e['reason'] == 'expanded_after_reference' for e in events)
                assert backend.sample('QA1L:currentRead').scalar() == 3
                assert backend.sample('QA1L:currentWrite').severity == 3
                assert backend.sample('CM1L:phaseRead').scalar() == 12
                assert backend.sample(DR_MODE).scalar() == 1
                assert backend.sample(DR_TURN).scalar() == 7
                for pv in (DR_MODE, DR_TURN):
                    try:
                        backend.put(pv, 2)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError('Diagnostic write allowed')
                records = [json.loads(x) for x in (runner.journal.directory/'commands.jsonl').read_text().splitlines()]
                commands = [r for r in records if r['event'] == 'command_intent']
                assert commands
                assert all(r['pv'] in ('CM1L:phaseWrite', 'QA1L:currentWrite') for r in commands)
                for record in commands:
                    limit = .1 if record['pv'] == 'QA1L:currentWrite' else 1.
                    assert abs(record['requested'] - record['before']) <= limit + 1e-8, record
                backend.close()
                backend = None
                # Exercise actual Qt connection/acquisition worker threads too.
                from PyQt6.QtWidgets import QApplication
                from Knobs.RandomExcitation.gui import MainWindow
                app = QApplication.instance() or QApplication([])
                window = MainWindow(transport_factory=lambda: EpicsTransport(pv_factory=pv_factory))
                def pump_until(predicate, timeout=12):
                    deadline = time.monotonic() + timeout
                    while not predicate() and time.monotonic() < deadline:
                        app.processEvents()
                        time.sleep(.01)
                    app.processEvents()
                    assert predicate(), window.status.text()
                try:
                    window.output.setText(folder)
                    window.guard.setValue(.05)
                    window.samples.setValue(3)
                    window.max_points.setValue(20)
                    window.connect_button.click()
                    pump_until(lambda: window.connector and not window.connector.isRunning())
                    assert window.transport is not None, window.status.text()
                    assert {a.name for a in window.selected()} == {'L1', 'QA1L'}
                    window.start_button.click()
                    def request_initial(data):
                        if data['state'] == 'point' and data['visits'] == 2:
                            window.restore_button.click()
                    window.worker.update.connect(request_initial)
                    pump_until(lambda: window.worker and not window.worker.isRunning())
                    qt_measurements = window.worker.runner.visits
                    assert 2 <= qt_measurements < 20
                    assert window.status.text() == 'Finished · Initial settings restored', window.path_label.text()
                    assert window.transport.sample('QA1L:currentRead').scalar() == 3
                    assert window.transport.sample('CM1L:phaseRead').scalar() == 12
                finally:
                    if window.worker and window.worker.isRunning():
                        window.worker.runner.stop()
                        window.worker.wait(5000)
                    window.close()
                    app.processEvents()
                print(json.dumps(dict(status=result, measured_points=len(points), commands=len(commands),
                                      current_limits=[p.lower, p.upper], dr_mode=1, dr_turn=7,
                                      qt_worker_measurements=qt_measurements, qt_initial_restored=True,
                                      transport='real PyEPICS / caproto / loopback only')))
            except Exception:
                if backend:
                    print('TEST connection state:', {n: (backend._pvs[n].connected, str(backend.sample(n)))
                          for n in ('QA1L:currentWrite', 'CM1L:phaseRead', 'BIM:DR:nparticles')})
                log.flush()
                log.seek(0)
                print('TEST IOC output:', log.read()[-7000:])
                raise
            finally:
                if backend:
                    backend.close()
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == '__main__':
    main()
