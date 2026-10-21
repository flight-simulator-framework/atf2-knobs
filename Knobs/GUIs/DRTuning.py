"""Compatibility launcher; preferred invocation: python -m Knobs.DRTuning."""
if __name__=='__main__':
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from Knobs.DRTuning.__main__ import main
    raise SystemExit(main())
