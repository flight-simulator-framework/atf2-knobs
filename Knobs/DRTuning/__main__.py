import sys
from PyQt6.QtWidgets import QApplication
from .gui import DRTuningWindow

def main():
    app=QApplication(sys.argv);window=DRTuningWindow();window.show();return app.exec()

if __name__=='__main__':raise SystemExit(main())
