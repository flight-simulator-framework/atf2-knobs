"""Compatibility launcher alongside the other Knobs GUIs."""
if __name__=='__main__':
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from Knobs.DRCorrection.__main__ import main
    raise SystemExit(main())
