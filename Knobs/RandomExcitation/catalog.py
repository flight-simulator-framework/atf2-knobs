"""PV definitions checked against LinacOpt and the ATF2 interfaces.

No interface is instantiated here. Generic get_icts() in the interfaces returns
dummy values; ICT_PVS follows InterfaceATF2_Linac.read_icts_for_optimizer().
"""
from dataclasses import dataclass
from Knobs.timing_pv import TIMING_PV

ICT_PVS = {name: f"BIM:{name}:nparticles" for name in ("GUN", "LN0", "LNE", "BTM", "BTE", "DR")}
DR_MONITORS = "DR:monitors"
DR_MODE = "DRBPM:ORBIT_MODE"
DR_TURN = "DRBPM:INJ:TURN_COUNT"
DR_BPM_NAMES = tuple(f"MB{i}R" for i in range(1, 99) if i not in (20, 75))
XSR_PVS = {key: f"XSR:PROJ_{key}_AVE" for key in ("V_SIGMA", "H_SIGMA", "V_MEAN", "H_MEAN")}
DIAGNOSTIC_PVS = tuple(ICT_PVS.values()) + (DR_MONITORS, DR_MODE, DR_TURN) + tuple(XSR_PVS.values())

GROUPS = {
    "phase": "RF phase / GUN, L0–L8",
    "solenoid": "Solenoid",
    "qa": "Linac QA1–QA5",
    "qm": "Linac QM1–QM3",
    "timing": "Injection timing",
    "linac": "Linac correctors",
    "bt": "BT correctors",
    "dr": "DR correctors (optional)",
}
BT_CORRECTORS = tuple("ZX10T ZX11T ZH10T ZV11T ZX12T ZY20T ZY21T ZY22T ZY23T "
                      "ZX30T ZX31T ZV30T ZH30T ZX32T ZV40T ZH40T "
                      "ZX50T ZX51T ZX52T ZV50T ZH50T ZV51T".split())
# Exact unique names in InterfaceATF2_DR.sequence, not an assumed complete ring.
DR_CORRECTORS = tuple([f"ZH{i}R" for i in range(1, 49) if i != 39]
                      + [f"ZV{i}R" for i in range(1, 52) if i != 42])


@dataclass(frozen=True)
class Actuator:
    name: str
    group: str
    kind: str
    writes: tuple[str, ...]
    read: str | None
    feedback: str = "readback"

    @property
    def unit(self):
        return {"phase": "deg", "current": "A", "timing": "ns"}[self.kind]

    @property
    def limit_pvs(self):
        return (f"{self.name}:parameter.A", f"{self.name}:parameter.B") if self.kind == "current" else ()

    @property
    def sources(self):
        return tuple(dict.fromkeys(self.writes + ((self.read,) if self.read else ()) + self.limit_pvs))


def catalog():
    items = [Actuator("GUN", "phase", "phase", ("RFGUN:PHASE_WRITE",), None, "command echo"),
             Actuator("L0", "phase", "phase", ("CM0L:phaseWrite",), None, "command echo")]
    items += [Actuator(f"L{i}", "phase", "phase", (f"CM{i}L:phaseWrite",), f"CM{i}L:phaseRead")
              for i in range(1, 9)]
    items += [Actuator("SOLENOIDE", "solenoid", "current", ("SOLENOIDE:currentWrite",),
                       "SOLENOIDE:internalCurrentWrite", "internal command readback")]
    for group, names in [("qa", [f"QA{i}L" for i in range(1, 6)]),
                         ("qm", [f"QM{i}L" for i in range(1, 4)]),
                         ("linac", [f"Z{plane}{i}L" for i in range(1, 13) for plane in ("H", "V")]),
                         ("bt", BT_CORRECTORS), ("dr", DR_CORRECTORS)]:
        items += [Actuator(n, group, "current", (f"{n}:currentWrite",), f"{n}:currentRead") for n in names]
    items.insert(19, Actuator("Timing", "timing", "timing", (TIMING_PV,),
                              None, "delay command echo"))
    return tuple(items)


CATALOG = catalog()
BY_NAME = {a.name: a for a in CATALOG}
