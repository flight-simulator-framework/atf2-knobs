"""python -m Knobs.DRCorrection [--backend demo|stored|epics]."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(description='Python op-base DR correction GUI')
    parser.add_argument('--backend',choices=['demo','stored','epics'],default='demo')
    parser.add_argument('--profile',type=Path,help='Confirmed hardware PV profile for explicit EPICS mode')
    parser.add_argument('--run-directory',type=Path,help='Directory for stored-DR simulation measurements')
    parser.add_argument('--seed',type=int,default=32)
    args=parser.parse_args(argv)
    from .backends import DemoBackend,StoredBackend,EpicsBackend
    if args.backend=='epics':
        if args.profile is None:parser.error('--backend epics requires --profile')
        backend=EpicsBackend(json.loads(args.profile.read_text()))
    elif args.backend=='stored':
        if args.run_directory is None:parser.error('--backend stored requires --run-directory')
        args.run_directory.mkdir(parents=True,exist_ok=True)
        backend=StoredBackend(args.run_directory,seed=args.seed)
    else:backend=DemoBackend()
    from PyQt6.QtWidgets import QApplication
    from .gui import DRCorrectionWindow
    app=QApplication(sys.argv[:1]);window=DRCorrectionWindow(backend);window.show()
    return app.exec()


if __name__=='__main__':raise SystemExit(main())
