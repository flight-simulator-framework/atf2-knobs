"""Bundled ATF control-room defaults; importing/loading never accesses EPICS."""
import json
from pathlib import Path


DEFAULT_PROFILE = Path(__file__).with_name('hardware_profile.default.json')


def load_hardware_profile(path=None):
    """An optional complete site profile replaces the bundled defaults."""
    return json.loads((Path(path) if path and str(path).strip() else DEFAULT_PROFILE).read_text())
