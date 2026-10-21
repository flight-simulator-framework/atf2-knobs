DR correction GUI — Python port of the op-base panels
====================================================

2026-10-21 follow-up: see HARDWARE_SOURCE_AUDIT.txt for recovered SysID/DeltaFRamp
controls, per-magnet :parameter.A (lower) / .B (upper) limits, TBT um units and
historical beta coefficients, and the unresolved COD/TBT name mapping. The
hardware profile now routes RF scans through RAMP presets. Earlier source
limitations described below are superseded where that audit provides evidence.

Launch from flight-simulator-framework:

  PYTHONPATH=atf2-knobs /home/motokisato/rftrack-env/bin/python -m Knobs.DRCorrection

This starts a synthetic DEMO, with no EPICS connections. The banner identifies
the backend. The alternate launcher is Knobs/GUIs/DR_Correction_GUI.py.
Required packages: PyQt6, numpy, scipy, matplotlib, machine-interfaces-atf2
(the existing ATFDRRingCorrection proposal solver). Stored simulation also uses
the workspace simulation-agent dependencies in rftrack-env.

Existing 200 ms stored-DR simulation:

  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=atf2-knobs \
    /home/motokisato/rftrack-env/bin/python -m Knobs.DRCorrection \
    --backend stored --run-directory /tmp/dr-correction-session

The stored adapter uses StoredDRInterface, seed 32, 5 um RMS alignment errors,
and the qualified operational_madx stored-beam initial distribution. It does
not expose the lattice, covariance or emittance truth to the GUI. Longitudinal
positions in plots use explicitly labelled nominal geometry from the old TBT
plotting script. They are not measured beta functions or calibrated live slots.

Ported screens and workflow
--------------------------
1. DR COD correction: beam energy/tune reference fields, H/V plane selection,
   number of steerers, tolerance, CALC / CALC(FILE) / SET / RESET, help.
2. DR dispersion correction: total frequency span in kHz, nominal momentum
   compaction, joint dispersion/COD fit, X/Y dispersion and X/Y COD plots.
3. Skew correction: two H-steerer excitations, total current spans, SD/SF family
   selection, two vertical orbit-difference plots. SD/SF SK1/I fields remain
   visible as reference metadata; the measured response determines correction.
4. TBT beta: waveform, FFT, fitted tune, phase, relative or independently
   calibrated absolute beta, statistics, file input, data and figure output.
   Simulation excitation can be selected separately in H or V. Weak signals
   fail the existing five-noise-RMS gate instead of displaying invented beta.
5. BPM DATA TO SAD: read/load A/B/standard buffers, B-A by BPM name, selection,
   JSON/CSV and plot output. Safe numeric ATFBPM parsing supports old SAD files
   with an explicitly supplied M.i -> physical BPM mapping. Source m -> mm.
6. FIX DR STEER: selectable BPM exclusions and fixed/free correctors/skews.
7. SRMON: fit a supplied public response basis to both dispersions; evaluate
   that same basis at SR/SR2; display observed points/fitted curves/monitor
   markers; save numerical data and PNG/PDF. A synthetic calibration is
   available only with the DEMO backend. Real calibration must be supplied.

CALC takes temporary symmetric measurements and restores the pre-scan settings.
CALC cleanup restores only the probes actually operated (including RF and skew
excitation correctors); it does not replay the full machine snapshot. SET/RESET
still use the full snapshot for rollback/undo and need separate commissioning.
It uses the existing ATFDRRingCorrection.propose_orbit_correction,
propose_dispersion_correction and propose_coupling_correction functions with
measured arrays substituted for model-side measurements. No RF-Track model
truth is used in these proposals. The saved session records the actual solver.
Near an absolute current limit, an asymmetric bounded extension handles the
remaining range. Corrector limits are +/-5 A. Simulation skew values are K1L
[1/m], limited to +/-0.001; live skew settings are amperes with required explicit
limits. These are never silently converted using an assumed A-to-K1L factor.

SET applies the displayed proposal only if backend and starting settings still
match; then it remeasures. Duplicate SET is refused. RESET restores the most
recent pre-SET snapshot (shared undo across tabs). Cancellation and acquisition
exceptions restore probes; failed remeasurement rolls back SET. Readback errors
remain visible in the log; successful software restoration is not a guarantee
against a hardware fault. CALC itself moves magnets/RF temporarily on live mode.

Live corrector/skew writes verify current readback, without an extra fixed wait
(magnet_settle_s defaults to 0, including profiles omitting it). This follows
SysID's separation of current convergence and repeated BPM acquisition.
SysID's InterfaceATF2_DR.get_bpms uses configurable nsamples and a default 1 s
interval; State.get_orbit forms means and population standard deviations.
DRTuning's live correction panels default to 3 samples at 1 s, adjustable in
the main panel or advanced settings. Three is a DRTuning initial setting, not
a claim about the current SysID GUI's selected nsamples. Simulation/API Options
retain one sample with zero delay unless explicitly configured otherwise.
DRTuning waits BEFORE each read (including the first), unlike SysID's sleep
after each read. Both schedule N * interval seconds. This avoids taking the
first sample immediately after a current change. Cancellation interrupts these
sample waits and still restores the probes. Correction datasets save raw X/Y/
intensity samples, validity, means, standard deviations, and N/interval for each
orbit measurement, including SET remeasurements. All samples must be valid for
a BPM to be fitted; a bad sample is not silently averaged into a valid BPM.
Repeated samples are not proof of fresh independent shots: COD acquisition
does not yet verify waveform timestamps/shot IDs (freshness_verified=false).
If an IOC stops updating, averaging alone cannot make its waveform fresh.
CALC progress states the requested current and settling interval. A zero
response now gives an explicit diagnostic with plane, valid BPM count and
per-magnet response RMS; nonzero responses with no useful correction have a
separate diagnostic. Neither case applies a correction or enlarges the probe.

First live dispersion response test (DRTuning GUI):
- Use the updated code and restart the GUI. Select COD mode in the existing
  machine controls first; the dispersion reader checks but does not select it.
- Plane: Vertical (Y). In Select BPMs / magnets, leave only one appropriate
  vertical corrector checked in Magnets (checked = free); keep usable BPMs.
  Maximum V correctors limits the solver, not the response acquisition count.
- Probe +/- [A]: 0.01. RF full span [kHz]: 2 (current offset +/-1 kHz).
  Use the established machine momentum compaction; the provisional default is
  0.0021. Ensure the RF endpoints are in the allowed offset range (+/-5 kHz).
- Record the corrector current and DeltaFRamp state/readback. Measure response
  (CALC) performs three RF pairs: original current, current-0.01 A, and
  current+0.01 A. With the unchanged 5 s RF settling and 3 BPM samples at 1 s,
  this includes 45 s RF settling + 27 s BPM sampling, plus ramp/readback time.
  COD-only CALC with one corrector schedules 9 s BPM sampling (3 settings *
  3 samples * 1 s), without additional magnet settling. No correction is
  applied by CALC.
- Check restoration and save data/plots. Stop this first test before SET/RESET.
  Failure: retain the detailed log and verify the current/RF restoration.

Selection controls and optics constants
--------------------------------------
Select BPMs / magnets provides All, None, name filtering (space-separated OR),
Check/Uncheck matches, and Only highlighted (Ctrl/Shift for multiple rows).
H only / V only / SD only / SF only replace the complete magnet selection.
Filtering alone never changes checks; checked items hidden by the filter are
counted explicitly. OK applies the choices to COD/Dispersion/Skew together;
Cancel leaves the existing choices untouched. Initial checks remain unchanged.
For one magnet, highlight its name and use Only highlighted. The main DRTuning
panel reports the actual CALC magnet count and number of RF pairs before a scan.
Choices last for the session; no machine-specific good/bad list is invented.

Momentum compaction is displayed read-only on the main DRTuning panel, default
0.0021 as used by InterfaceATF2_DR.change_energy. For a change of optics, edit it
under SVD / advanced settings. It is stored in each measurement session, and is
not estimated by the dispersion measurement. This GUI does not claim that the
default has been measured for the currently operating lattice.

Comparison with existing acquisition/correction programs
-------------------------------------------------------
SysID_GUI.py collects corrector response data in nominal and changed-energy
states, scanning the correctors within each energy state. Its current DR
interface change_energy uses the DeltaFRamp PL4 preset (+4 kHz), with 0.0021
momentum compaction. It writes DATA*.pkl for subsequent response-matrix/BBA
processing; DRTuning does not call that worker or consume those files directly.

DRTuning measures a symmetric RF pair at nominal current and again at both
probe currents for each enabled corrector: 1+2*N RF pairs. It fits dispersion
and nominal COD jointly using measured response columns, greedy selection,
SVD cutoff and current bounds. Vertical dispersion and COD targets are zero;
horizontal dispersion requires its nominal target. Defaults multiply the
dispersion residual by 0.05 and the COD residual by 1 before least squares.
This ordering costs more RF transitions than grouping scans by energy.

The recovered SAD operation/lib/coddispersion.n also jointly fits dispersion
and COD (factor1=0.05) and selects steerers subject to limits. However its
response columns are generated by perturbing K0 inside the SAD lattice and
recalculating Twiss/COD, not by physically probing every magnet. It also has
model/tune fitting, regional targets and extraction constraints that are not
reproduced by the current DRTuning measured-response solver. The GUI and solver
concepts are ported; it is not an identical execution of the original SAD path.

Save data + figures writes JSON, CSV, PNG and PDF. The toolbar also supports
zoom, pan and image export; Save figure supports SVG. Correction CSV contains
the fitted signals plus both full-plane observations. TBT saves JSON and NPZ.
The blue curve is the fitted correction component, not a claim that the nominal
SAD lattice optics have been re-fitted. Predicted and measured-after curves are
shown separately; an ineffective correction is retained in the saved results.

Deliberate differences from the old SAD applications
--------------------------------------------------
- The original three correction .ui layouts/object names/text are retained in
  forms/. QE widgets are replaced with ordinary PyQt6 widgets. Source paths,
  SHA256 values and original SAD parameter PVs are in legacy_inventory.json.
- H or V is corrected one plane per proposal; both are plotted. Perform a
  separate CALC/SET for the other plane. The original independent H/V switches
  are represented by mutually exclusive active-plane buttons.
- Beam energy/tunes and SK1/I are reference metadata in measured-response mode.
  Changing them does not retune a hidden lattice or change RF/magnet hardware.
  Tolerance is used by the asymmetric bounded extension; the existing symmetric
  port uses its own greedy selection termination. No exact SAD numerical
  identity is claimed. The stored session's solver/options are authoritative.
- CALC(FILE) re-solves a Python response-session JSON without acquiring data.
  It requires the recorded plane. Old SAD orbit buffers can be loaded/compared
  in BPM DATA TO SAD; they alone do not contain a response matrix for CALC(FILE).
- TBT analysis is the previously ported sine-fit analyzer (source provenance in
  tbt.py), not the old PyNAFF procedure. The old numerical beta calibration is
  not reused for a different amplitude convention. Absolute beta requires an
  independent beta at a reference BPM. Legacy X/Y/Wint text matrices, including
  the bpmNN_x/y/wint header, can be imported with explicit units and noise.
- SR fit requires the current public response calibration. It does not rebuild
  the old SAD tune-fitted lattice from its .n files. The response-basis fit is
  tested independently with a known synthetic basis.
- The old Turn1024 acquisition automation, extraction-shot service and remote
  XSR beta-fit executables are not newly ported here. Their physics/control
  services were not part of the previously ported correction functions.
- The live RF offset dialog now selects DeltaFRamp presets, with the linked
  machine operation delegated to the existing controller as in SysID. Presets
  are integer kHz; raw frequency readback is MHz. Simulation remains continuous.
- No SAD parameter/status PV or existing result waveform is written by default.
  These Python controls are local. Site permission checks from the old Qt
  application are not reproduced; a live rollout must use the site's approved
  control/access setup. No real machine was connected during this work.

Hardware profile and outstanding site information
-------------------------------------------------
hardware_profile.example.json is deliberately incomplete. The EPICS constructor
validates it before importing/connecting EPICS. Explicit --backend epics
--profile <confirmed.json> is required; pyepics is needed only for that backend.

Already recovered from existing code:
  COD: DR:monitors, 10 values/slot, status/x/y/intensity in columns 0/1/2/3,
       raw x/y divided by 1000 -> mm; numbered MB1R..MB98R except 20 and 75.
       DRBPM:ORBIT_MODE is checked against configured COD mode 1.
  Corrector: <name>:currentWrite and <name>:currentRead, +/-5 A.
  Skew examples: SD1R_1:internalCurrentWrite / SD1R_1:current (and SF), with
       lower/upper limits from SD1R_1:parameter.A / .B.
  RF readback: FREQUENCY:DR_read in MHz. Live scans use RAMP:* presets.
       The separate manual frequency-with-water operation also uses
       FREQUENCY:LN_write/read and FREQUENCY:DR_write_CALC[.PROC].
  TBT: bpm01..96.WPSH, .WPSV, .WINT; crate TURN/SNAP and shot-mode controls.
  SAD calculation/result PVs: forms/legacy_inventory.json.

Questions sent to the user after GUI generation:
  1. Confirm current COD/TBT channel-to-BPM mapping and longitudinal positions.
     The COD slot list and old nominal TBT BPM labels are not assumed identical.
  2. Confirm independent DR frequency control versus linked LN/DR/water control,
     frequency units, processing/settling steps, and skew current ranges.
  3. Locate current TBT units/acquisition start-completion handshake, absolute
     beta calibration, and the public SR/SR2 optical response model.

SR calibration JSON format (all dispersion values mm per dp/p):
  schema: "dr-sr-response-1"
  source: calibration provenance; names: ordered BPM names
  monitors: [{name: "SR", s_m: ...}, {name: "SR2", s_m: ...}]
  planes.x / planes.y each contain:
    bpm_response: [nBPM, nBasis], monitor_response: [nMonitor, nBasis]
    nominal_bpm: [nBPM], nominal_monitor: [nMonitor] (optional, defaults zero)
  Both matrices in a plane must use the same calibrated basis. A fit with
  insufficient BPMs or deficient rank is rejected. No interpolation by s alone.

Validation
----------
  QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/tmp/mpl-dr-gui \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=atf2-knobs \
    /home/motokisato/rftrack-env/bin/python -m unittest discover \
    -s atf2-knobs/Knobs/DRCorrection/tests -v

2026-10-21: full suite 26 tests passed; final focused controls suite 6 passed
(27 distinct tests covered). Includes ported-solver correction, limits, stale and
duplicate SET, cancellation and failed-scan restoration, failed-SET rollback,
file re-solving, numeric SAD parsing, TBT fits, SR response fit, fake EPICS packed
COD/per-BPM TBT mapping, failed writes, and offscreen Qt button/save workflows.
EPICS tests use a fake client only. Qt offscreen emits the normal
"This plugin does not support propagateSizeHints()" message.

Reproducible offline evidence:
  python -m Knobs.DRCorrection.validate --backend demo --output <directory>
  python -m Knobs.DRCorrection.validate --backend stored --output <directory>

Current artifacts: validation/demo-20261021 and validation/stored-20261021.
Saved correction screenshots were re-rendered offline after a plot layout fix;
the numerical measurement sessions were not re-run for this cosmetic change.
Stored integration: seed 32, two V correctors for COD/dispersion, one enabled SD
skew (from a two-family candidate subset), 44 orbit acquisitions + one TBT.
All CALC and RESET settings restored. Wall time ~7.9 s, measurement-time estimate
225 s (3.75 min); this is an integration smoke run, not a full optimization.
  COD Y RMS: 0.112150 -> 0.085085 mm.
  Dispersion Y RMS: 2.190509 -> 1.457814 mm per dp/p.
  Skew probe differences RMS: 0.000887/0.000818 -> 0.000953/0.000877 mm;
      no improvement demonstrated by this small/noisy skew trial.
  H-excited TBT: x tune 0.205298 valid; y estimate rejected by the signal gate.
These GUI integration results do not establish an emittance improvement.
