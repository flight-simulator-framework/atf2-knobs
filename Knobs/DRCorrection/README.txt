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
