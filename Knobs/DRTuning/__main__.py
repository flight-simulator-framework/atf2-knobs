import sys
from PyQt6.QtWidgets import QApplication, QMessageBox

from .epics import LiveBackend
from .gui import DRTuningWindow, TuningSession
from .hardware import load_hardware_profile


def main():
    app=QApplication(sys.argv)
    if '--select-interface' in sys.argv:
        selector=DRTuningWindow()
        if not selector.exec():return 0
        window=selector.session
    else:
        try:
            backend=LiveBackend(load_hardware_profile())
            try:window=TuningSession(backend)
            except Exception:
                backend.close()
                raise
        except Exception as exc:
            QMessageBox.critical(None,'DRTuning — EPICS',str(exc))
            return 1
        window.show()
    window.load_latest_matrices()
    return app.exec()


if __name__=='__main__':raise SystemExit(main())
