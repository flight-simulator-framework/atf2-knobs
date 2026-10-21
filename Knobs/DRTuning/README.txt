DRTuning — RFTrack / machine interface launcher
==============================================

Start from flight-simulator-framework:

  PYTHONPATH=atf2-knobs python -m Knobs.DRTuning

Or launch Knobs/GUIs/DRTuning_GUI.py directly with the same Python interpreter.
After package installation, the DRTuning GUI entry point is also available.
Select Interface, then Connect / initialize. Nothing connects to EPICS or writes
machine settings merely by opening DRTuning. The selection is locked while a
session is connected. Disconnect before choosing another interface.

RFTrack: native RF-Track transverse DR, reference 20111111b lattice, no radiation
or 200-ms equilibrium assertion. BPM positions come from tracked probes, with
1 um RMS simulated read noise. Quadrupole main coils hold design strengths;
individual trims use the existing mag_ki_main calibration and simulated +/-2 A
limits. Correctors retain the existing calibration and +/-5 A current bounds.
This is a diagnostics port validation environment, not a new emittance result.

Machine (EPICS): select 実機 (EPICS), then Connect / initialize. With the profile
field empty, hardware_profile.default.json is loaded automatically from this
package. No JSON selection or user-specific path is needed. Initialization loads
configuration only; individual measurements check their PV connections.
The optional profile picker REPLACES the defaults with a complete site JSON.
An invalid override is reported, never silently replaced by the defaults.
Stored DR (200 ms): retains the previous fast stored-beam corrections/TBT adapter;
this optional backend does not yet expose quadrupole trims. It is NOT RFTrack.
DEMO: synthetic correction/TBT data for UI checks, not quadrupole commissioning.

One interface is shared by COD, dispersion, skew, TBT, four Q-modulation panels,
BPM buffers, fixed-steerer selection, Delta-f Ramp and SR/SR2 dispersion fit.
They open in one session window so scans cannot race each other. Closing that
window hides it; Disconnect closes its interface. An active scan must finish
restoration before disconnecting. Cancellation is available during Q modulation
and TBT acquisition. Existing response CALC/SET/RESET behavior is retained.

Q modulation
------------
The original QF1R (28), QF2R (26), QM trim 1 (23), QM trim 2 (23) .ui geometry,
widget names, five points per magnet, USE/OFF switches, GET/CALC/SAVE are ported.
QE plugins are replaced by local Qt controls; no old BETA:* result PV is written.
Original files, widget/PV inventory and SHA256 are in forms/inventory.json.

GET captures current and measured H/V tunes. Point-value buttons edit recorded
numbers, not hardware. USE/OFF excludes a magnet or individual fit point.
CALC fits Q vs calibrated K1L and returns |4 pi dQ/dK1L|, as prog_beta_calc.
The signed slopes, residuals and regression uncertainty are saved as well.
Beta is magnet-averaged, not automatically a nearby BPM's beta.

Scan 5 points / restore is an added automated acquisition. Choose a magnet and
a FULL current span; it takes five symmetric points around the current setting.
All targets and tune prerequisites are checked before writing. Current is
restored on success, cancellation, or an acquisition/fit error. Per-supply live
limits use :parameter.A (lower), :parameter.B (upper). The simulation +/-2 A
trim limit is not an assertion about real supply limits. Plots and JSON/CSV/
PNG/PDF include recorded currents, calibration, tunes and fit results.

In RFTrack the tunes are fit from separately H/V-excited native BPM waveforms
(256 turns each), not taken from model eigenvalues. Existing physical current
calibration drives the one-turn excitation. Native tuning does not implement
real exciter hardware.

Live Q modulation defaults to existing TuneDR TBT:TUNE:H / TBT:TUNE:V PVs.
Both IOC timestamps must advance after the request; H/V timestamp separation
must satisfy max_timestamp_skew_s (provisional 0.5 s). Neither unchanged tune
values nor time.sleep alone are used as freshness evidence. mode=tbt instead
fits newly acquired BPM waveforms; it requires external H/V excitation.

The user accepted existing mag_ki_main calibration provisionally. library=null
builds workspace libmagnet C sources into a content-addressed /tmp library;
a site library can be selected explicitly. Main current and trim current are
passed separately, including the QM7R.1 -> QM7AR special case. Optional
field_check reads site blWrite/blWriteCalc-related values and records/compares
against K1L. Supply actual PV names and explicit k1l_per_unit conversion: a BL
PV's units and main-versus-trim meaning are not guessed. A check disagreement
aborts a scan and restores the original current.

TBT / beta
----------
The Turn1024 .ui layout is also retained, with its plot area replaced by an
interactive Matplotlib plot. Update, BPM previous/next, X/Y/intensity selection,
turn/step, plot limits, statistics, automatic saving, beta calculation, plot
windows and file operations are connected. SCAN(+/-) advances the selected
first turn on each Update. This port does not subscribe a perpetual background
loop to every beam pulse. The extraction checkbox selects a configured
extraction_turn; it does not start the old external extraction-shot daemon.
TESTMODE is represented by the explicit DEMO interface at the launcher.

RFTrack starts at turn 1; choose Fit start turn to omit the excitation passage.
It does not track 471150 native turns to emulate the legacy extraction setting.
The optional stored backend is the separately qualified 200-ms model.

Amplitude beta: Beta at BPM CALC uses the existing sine-fit analyzer. Relative
beta is the default. Absolute beta from independent reference requires a real
calibration at the selected BPM; the initial value 8 m is NOT a calibration.
Historical PyNAFF scale constants are not applied to different excitation.

Phase beta: Three-BPM phase beta uses measured phase advances and separately
supplied nominal transport ratios. forms/nominal_phase_optics.json is generated
from an independent unperturbed 20111111b lattice, auto-loaded ONLY for RFTrack.
It is never generated from the connected perturbed machine. Weak BPM signals,
near-singular phase advances and nonpositive beta are rejected. The first BPM
of each accepted triplet is the measurement location; no ring-wrap phase is
silently assumed for the last BPMs. Output includes accepted/rejected triplets.
Regenerate the public design calibration using:

  python -m Knobs.DRTuning.nominal /tmp/nominal_phase_optics.json

Live acquisition without a completion PV
---------------------------------------
The user confirmed that there is no dedicated all-BPM completion PV. Default
readback_checks therefore ports the Turn1024 one-shot / shot-mode / TURN / SNAP
sequence and uses TBT:TUNE:H timestamp updates as the old event source. It then
processes the BPM records, requires every waveform timestamp to advance, checks
that waveform content changed, and requires two stable reads while the event
timestamp remains unchanged. Stale, partial and torn reads time out. All original
mode/turn values are restored, also on failure or cancellation.

These are SOFTWARE readback checks, not proof of a common hardware shot ID.
Every resulting measurement has same_shot_verified=false and records the IOC
timestamps/content hashes. The GUI and Q-modulation results state this limitation.
A future genuine completion counter can use mode=completion with generation_pv
and completion_confirmed=true. Never point it at a tune value and claim it is
an acquisition counter. Acquisition settling defaults (1 s arm, 1 s readout,
50 ms stable reread, 30 s timeout) are provisional settings, not measured machine
performance. Read-only previously acquired waveforms remain available by
unchecking Acquire new shot.

Calibration files and remaining site configuration
-------------------------------------------------
hardware_profile.default.json bundles the existing interface/op-base convention:
96 COD BPMs (MB1R..MB98R except 20/75), the interface's 47 horizontal and 50
vertical correctors, 34 SD and 34 SF supplies, 100 quadrupole trims, TuneDR and
Turn1024 PVs. Current limits are read from parameter.A/B at use time, with the
additional +/-5 A corrector limit. Q modulation uses prog_beta_calc's main-coil
internalCurrentWrite reference, including QM7R.1 -> QM7AR. This source-based
configuration has not been commissioned against live hardware here.

The default RF/quad settling delays are 5 s; RF tolerance is 100 Hz and timeout
30 s. These are configurable software defaults, not measured machine response
times. RF uses the linked DeltaFRamp controller and MHz readback conversion.
Missing horizontal dispersion targets or live phase/SR optics are requested by
their respective analyses; they do not block startup or unrelated tools.

MB97R/MB98R longitudinal positions are not present in the bundled nominal
lattice. They stay null (allow_missing_positions=true); COD plots then use a
labelled BPM-index axis. Measured response corrections do not need s positions.
TBT keeps its separate legacy 96-channel nominal geometry. No physical position
is invented and no COD/TBT channel is silently relabelled.

hardware_profile.example.json provides candidate device names but deliberately
keeps operational mapping/settling values unconfirmed. Confirm only the supplies
you use; unconfigured quads do not prevent unrelated correction tools from use.
DR:monitors includes slots through 98. The existing COD enabled list (20/75
omitted) and TBT-96 mapping remain separate in both profiles; waveform capacity
alone does not identify physical channels. The source conventions above are
used as defaults, without inventing a correspondence between these two lists.

Live phase beta needs a current public transport model in dr-phase-optics-1
format: source string, planes.x/y lists of {names:[i,j,k], m11_m12:[a,b],
phase_sign:+1|-1}. m11_m12 = M11/M12 from i to j/k, units 1/m.

Fit BETA near XSR: local constrained Courant-Snyder beta fit plus SR/SR2
prediction is implemented in sr_beta.py. It can consume absolute amplitude beta
or phase beta. Supply a JSON {planes:{x:<cal>,y:<cal>}}, where each cal has schema
"dr-sr-beta-1", source, bpms:[{name,r11,r12_m}], monitors:[{name,r11,r12_m}].
All transports must share the same reference location per plane. Full rank and
positive beta are required. Measured/fitted beta and monitor estimates are saved.

Exact equivalence to the external Okugi/Kubo SAD executables is NOT claimed:
Turn1024 calls scripts under /userhome/abe/SAD/ATF/ATF-DR/SRfit_Okugi which are not
in the available checkout. Their GUI buttons open the explicitly labelled local
fit. The available sad/operation/sr_betafit.sad similarly fits four initial Twiss
parameters, but the current SR_Betafit optical configuration still has to be
supplied. The sine-fit implementation is not the old PyNAFF implementation.

Verification (2026-10-21)
------------------------
New offline tests cover Q-modulation physics, limits/restoration/cancellation,
phase beta, SR beta fit, acquisition failure/staleness/torn data, live quad
readback/field checks, original GUI controls and a shared launcher session.
22 new tests and all 27 existing DRCorrection tests passed (49 total). No real
EPICS IOC was connected.

  QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/tmp/mpl-dr-gui \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=atf2-knobs \
  /home/motokisato/rftrack-env/bin/python -m unittest discover \
  -s atf2-knobs/Knobs/DRTuning/tests -v

Native reproducible integration (~32 s on this machine, not a learning run):

  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=atf2-knobs \
  /home/motokisato/rftrack-env/bin/python -m Knobs.DRTuning.validate \
  --output /tmp/drtuning-validation

QF2R.1 trim +/-0.02 A, five points, then H/V phase-beta measurements:
  measured magnet-averaged beta X/Y = 4.17079 / 1.22842 m
  independent reference average    = 4.20553 / 1.24062 m
  relative difference              = -0.826% / -0.983%
  phase beta accepted triplets     = 91 / 91
  phase beta vs public TFS RMS     = 0.774% / 1.041%
  all settings restored; native run wall time 32.46 s.
The offline reference audit uses optics solely to assess measurements. Neither
Q-modulation nor phase-beta analysis is fed the perturbed simulator's true beta.
This qualification covers one nominal lattice, not arbitrary coupling/errors.

Local artifacts: validation/native-20261021 (measurements, reference audit,
launcher and UI screenshots). Screenshots use an offline demo fixture; the Q
plot displays the saved native measurement. No screenshot is a live-machine run.
