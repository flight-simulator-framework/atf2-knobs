import sys
from PyQt6.QtWidgets import QApplication
from .gui import DRTuningWindow

def main():
    app=QApplication(sys.argv);selector=DRTuningWindow()
    if not selector.exec():return 0
    return app.exec()

if __name__=='__main__':raise SystemExit(main())
