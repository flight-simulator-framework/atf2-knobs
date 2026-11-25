"""Bundled ATF control-room defaults; importing/loading never accesses EPICS."""
import json
from pathlib import Path


QM_MAIN_CANDIDATES = tuple(f'QM{i}R' for i in (1, 2, 3, 4, 20, 21, 22, 23))
QM_CANDIDATES = tuple(f'{family}.{member}' for member in (1, 2) for family in QM_MAIN_CANDIDATES)


def main_supply_mappings(trims):
    """Build one main supply only when both trim mappings identify the same PVs and calibration."""
    supplies = {}
    for family in QM_MAIN_CANDIDATES:
        pair = []
        for member in (1, 2):
            trim = trims.get(f'{family}.{member}', {})
            cal = trim.get('calibration') or {}
            if not (cal.get('confirmed') and cal.get('kind') == 'mag_ki_main'
                    and cal.get('magnet') == family and cal.get('coil') == 'trim'):
                break
            pvs = {}
            for key in ('set', 'read', 'lower_limit_pv', 'upper_limit_pv'):
                pv = trim.get(key)
                if not isinstance(pv, str):
                    break
                device, _, field = pv.partition(':')
                if device not in (f'{family}_{member}', f'{family}.{member}') or not field:
                    break
                pvs[key] = f'{family}:{field}'
            if len(pvs) != 4 or not isinstance(trim.get('settle_s'), (int, float)):
                break
            pair.append(dict(pvs, calibration=dict(cal, coil='main'), mapping_confirmed=True))
        if len(pair) == 2 and pair[0] == pair[1]:
            supplies[family] = dict(pair[0], settle_s=max(trims[f'{family}.{i}']['settle_s'] for i in (1, 2)))
    return supplies


def quadrupole_supply(backend, name):
    if name in QM_MAIN_CANDIDATES:
        return backend.main_quadrupoles[name]
    return backend.quadrupoles[name]


DEFAULT_PROFILE = Path(__file__).with_name('hardware_profile.default.json')


def load_hardware_profile(path=None):
    """An optional complete site profile replaces the bundled defaults."""
    return json.loads((Path(path) if path and str(path).strip() else DEFAULT_PROFILE).read_text())
