import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="ATF random excitation and command logging")
    parser.add_argument("--demo", action="store_true", help="Synthetic signals; never connect to EPICS")
    args = parser.parse_args()
    from PyQt6.QtWidgets import QApplication
    from .gui import MainWindow
    app = QApplication(sys.argv[:1])
    window = MainWindow(demo=args.demo)
    screen = app.primaryScreen()
    if screen:
        size = screen.availableGeometry().size()
        window.resize(min(1400, int(size.width()*.95)), min(900, int(size.height()*.92)))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
