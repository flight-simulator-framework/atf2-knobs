"""Atomic, human-readable QM scan checkpoints; no hardware access."""
import json
import os
from pathlib import Path
import tempfile
from Knobs.DRCorrection.core import serial


def prepare_destination(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryFile(dir=path.parent) as handle:
        handle.write(b'checkpoint preflight');handle.flush();os.fsync(handle.fileno())


def save_checkpoint(path,data):
    path=Path(path);temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
                                         prefix=path.name+'.',suffix='.tmp',delete=False) as handle:
            temporary=Path(handle.name)
            json.dump(serial(data),handle,allow_nan=False,separators=(',',':'))
            handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def load_checkpoint(path):
    with Path(path).open(encoding='utf-8') as handle:data=json.load(handle)
    if not isinstance(data,dict) or data.get('checkpoint_schema')!='dr-qm-scan-1':
        raise ValueError('Not a resumable QM scan checkpoint')
    return data
