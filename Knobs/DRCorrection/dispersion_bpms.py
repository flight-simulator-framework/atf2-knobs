"""ATF damping-ring BPM regions for the DRTuning dispersion objectives."""
import re

import numpy as np


def mb_numbers(names):
    numbers = []
    for bpm in names:
        match = re.fullmatch(r'MB(\d+)R', bpm.removeprefix('dr:'))
        numbers.append(int(match[1]) if match else -1)
    return np.asarray(numbers, int)


def vertical_arc_bpms(names):
    n = mb_numbers(names)
    return ((1 <= n) & (n <= 18)) | ((32 <= n) & (n <= 64)) | ((81 <= n) & (n <= 96))


def cod_change_bpms(names):
    return np.isin(mb_numbers(names), (65, 66, 19, 22, 23, 24))
