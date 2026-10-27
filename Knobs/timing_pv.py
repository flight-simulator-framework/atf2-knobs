"""Timing access policy shared by display, tuning and random excitation."""

TIMING_PV = "EVE_LINAC:OUT0:SetData"


def validate_timing_pvs(names):
    """Reject obsolete register PVs before creating channels or writing values."""
    for name in names:
        if name and str(name).upper().startswith("EVE_LINAC") and name != TIMING_PV:
            raise ValueError(f"Timing PV access disabled: {name}; use {TIMING_PV} [ns]")
