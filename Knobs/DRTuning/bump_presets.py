"""Names verified in nominal RFTrack; loading never accesses the machine."""
import copy
import json
from functools import lru_cache
from pathlib import Path
from .model import name


@lru_cache(maxsize=1)
def _catalog():
    data=json.loads(Path(__file__).with_name('bump_presets.json').read_text())
    if data.get('schema')!='dr-bump-presets-1':raise ValueError('Unsupported bump preset schema')
    return data


def bump_preset(target):
    data=_catalog();entry=data['presets'].get(name(target))
    return dict(copy.deepcopy(entry),id=data['id']) if entry else None


def bump_targets():
    return list(_catalog()['presets'])
