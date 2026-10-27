"""Launcher discovered by deploy_atf2.sh alongside the other ATF GUI tools."""
import sys
from pathlib import Path

root = str(Path(__file__).resolve().parents[2])
if root not in sys.path:
    sys.path.insert(0, root)

from Knobs.RandomExcitation.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
