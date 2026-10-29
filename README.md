# atf2-knobs

ATF2 IPBSM optimization, beam tuning, linac optimization and controllers.

## Random excitation / BigData companion

Run `python Knobs/GUIs/RandomExcitation_GUI.py --demo` to try the new acquisition
GUI without EPICS. Omit `--demo` in the LinacOpt environment for machine access.
It offers the LinacOpt control groups, optional DR correctors, 1-degree phase
and 0.1-A current grids, verified current limits, persistent CA monitors and an
append-only command journal. DR diagnostic selectors are read-only. Pause
finishes the current measurement; Resume remeasures it, and Save and End applies
the best recorded settings. See the [operator guide](Knobs/RandomExcitation/README.txt)
for range selection, timing, logging, completion and validation details.

DR optics tools are available through `python -m Knobs.DRTuning` (or `DRTuning`
after installation). Select RFTrack or the configured machine interface, then
open COD, dispersion, skew, TBT beta or quadrupole-modulation measurements from
the shared launcher. See [DRTuning usage and validation](Knobs/DRTuning/README.txt).

## LinacOpt active feedback

The top-level tabs are **Main → Active Feedback → Config**. Active Feedback
runs until Stop, with Main
optimization and reset disabled while feedback owns the actuators. Start captures
a fixed initial transmission and ML8L/ML10L orbit plot origin. The correction
reference starts at that orbit and follows confirmed transmission baseline improvements.
When startup response measurement is enabled, its completion is followed by
fresh 10-shot transmission and BPM measurements and a reread of all setpoints.
These post-calibration values become the feedback Initial, recovery baseline,
orbit reference and parameter origins; the transmission threshold is recalculated
from the new baseline. Feedback cannot begin until this capture succeeds. An RF power
loss during capture pauses and retries capture on Resume without remeasuring
the successfully acquired matrix. Transmission is the
selected downstream ICT (DR by default) divided by LN0, independently of Main's
objective/weights. ICT-triggered automatic pause is enabled after 20 consecutive nonpositive shots. The normal feedback mode is timing tracking, with phase recovery
taking priority over orbit correction when transmission is degraded. Run Control selects **Timing**, **CM8L phase**, and **Correctors**
(all on by default). These target checkboxes remain editable while running.
Changes apply at accepted settings after any temporary probe has been restored;
disabled targets receive no new adjustment commands after the change is applied.
Applied selections are recorded as `targets_changed` events. A changed selection
wakes the cycle countdown and restarts dispatch with the new targets.
All targets unchecked displays **WAITING | No adjustment targets selected**,
retains the active run, and continues BPM/ICT monitoring and the RF power guard
without actuator writes or a scan countdown. Selecting a target resumes feedback
automatically. If Correctors
is off, timing and phase can still track even while the orbit
is outside tolerance. Timing off skips timing searches; phase recovery still uses
the transmission threshold at the current operating point. All Feedback Settings
are under **Config → Active Feedback** and remain locked during a feedback run.
With all targets enabled:

1. Track a local transmission maximum on the relative 2.8 ns timing grid using
   `EVE_LINAC:OUT0:SetData`. At the start of a cycle, existing actionable BPM or
   transmission recovery conditions bypass timing writes. Otherwise, try one
   neighbour at a time using an independent fresh 10-shot transmission mean at
   each point. Keep an improvement greater than the deadband (default 0.05
   percentage point) directly and continue in the same direction. The first
   non-improving step after an ascent restores the last accepted point and ends
   the search. If the very first step does not improve, restore the start,
   measure it again, and try the opposite direction. If neither direction
   improves, stay at the start. No extra acceptance confirmation or return to
   the centre is performed between improving steps. The first direction
   alternates with cycle parity. These rules also apply to the phase search.
2. After timing finishes or yields, take one fresh 10-shot transmission
   measurement. If it is **below recovery baseline − 0.05**, run CM8L phase
   recovery in 0.5° steps before any orbit correction, even if BPM drift exceeds
   its threshold. This is an absolute drop of **5 percentage points**; a baseline
   of 0.80 gives a threshold of 0.75. The drop and averaging count are adjustable
   in Config. Deliberate timing/phase probe measurements are excluded from the
   recovery average. Retune timing after phase recovery.
3. Continuously check timestamped `LINAC:monitors` records during acquisition,
   settling and cycle waits, averaging nonoverlapping batches of **10 distinct valid shots**.
   If any of ML8L.x, ML10L.x, ML8L.y or ML10L.y
   differs from its current orbit reference by at least the orbit threshold
   (150 µm by default, editable in Config), check phase recovery eligibility first.
   Recheck the orbit after phase/timing adjustments, then correct any remaining
   drift with ZH1L, ZV1L,
   ZH2L and ZV2L. Every changed corrector moves by 0.005 A per step. The threshold
   triggers correction even if transmission is not degraded. Timing yields at a
   non-improving probe; phase remains ahead of orbit correction. Confirm the drift using 10 fresh shots at the
   accepted settings before switching to correction. Correction targets the current orbit reference until
   no improving grid move remains, three trials fail consecutively, or the iteration limit is reached.
4. After phase/orbit correction, give timing a fresh neighbour trial so it can
   still improve even if transmission remains low. Continue confirmed improvements;
   after the directional search ends, check whether recovery is still needed. After orbit
   correction, evaluate transmission and run phase recovery if still degraded;
   an unresolved orbit does not block it. Orbit drift during the cycle wait starts
   another cycle with the same phase-before-orbit order.
   Pause retains accepted settings; Stop
   and closing the window remove temporary probes and retain accepted settings.

Orbit correction also intervenes **during timing and phase searches** whenever
Correctors is enabled. Monitoring latches drift continuously, and a fresh ten-shot
BPM mean confirms it at a completed RF evaluation boundary. Correctors remain
fixed throughout an RF reference/probe comparison and any rollback.
At the boundary, make at most one accepted 0.005 A correction step,
then return to the RF search. Timing can also intervene after its first failed
neighbour; a transmission drop still gives phase recovery priority there.
An intervention restarts the directional search at the retained RF setting,
discarding earlier comparisons and taking a fresh ten-shot reference mean at
the corrected currents. Already accepted RF settings remain.
This interleaving is disabled during dedicated response acquisition.

RMS history and failed-attempt counts for interleaved correction belong to the
same accepted timing/phase setting and orbit reference. After three consecutive
failed correction attempts, restore that context's best RMS settings, then let
the RF search proceed without repeating correction at that same setting in the
same cycle. A changed RF setting/reference or the next cycle permits another
attempt. The configured orbit step limit also bounds intervention at one RF
setting. `orbit_intervention` and `orbit_step` events record these actions.

The recovery baseline starts at the initial transmission. It rises only after
improvement is confirmed for **120 seconds and at least 3 complete operating-point
means**, with timing at a local optimum and normal BPM readings. The new baseline
does not advance from a timing search that yielded before confirming a local optimum.
The promoted value
is the lowest confirmed mean during that interval, avoiding a transient maximum.
The GUI's **Raise baseline after [s]** controls the duration. A confirmed mean at
or below the current baseline, Pause or beam loss cancels pending confirmation.
Orbit correction retains the candidate, elapsed time and confirmation count;
correction probes do not count, and promotion requires a subsequent complete
operating-point mean with normal BPM readings. The baseline never moves down;
for example, once it reaches 0.90,
phase recovery uses 0.85. Each transmission baseline promotion also sets the orbit
correction reference to the fresh 10-shot BPM mean at that operating point.
Subsequent drift detection and correction use this new reference; the tolerance
width stays unchanged. The initial transmission, orbit plot origin and parameter
origins remain fixed throughout the run. The BPM plot retains all measurements
relative to the initial orbit. Each coordinate's dotted limits, in the same color
as its measured trace, move to the new reference offset ± the configured tolerance
(default 150 µm). Baseline promotion events record the previous, new and initial
orbit references. A new START captures new initial references.

Run Control's **Measure response matrix at Start** checkbox (off by default)
acquires a new response before feedback when Correctors is enabled.
This checkbox can also be edited during a run; its changes apply at the next
START and do not request an immediate acquisition. Each corrector
starts with a 0.05 A trial, then adapts its excitation to give a 400–600 µm
change in the larger of the two primary-plane BPM readings (500 µm target).
Every trial uses fresh 10-shot means before excitation, at the displaced setting,
and after restoration. The response subtracts the average of the bracketing
reference measurements to reduce drift. Trials obey the configured/IOC limits;
if six attempts or the available current range cannot reach the target band,
acquisition stops with an error and retains the previous matrix. Each trial is
restored even on Stop, measurement failure or RF power loss. After RF auto-pause and
Resume, the startup acquisition retries before feedback can begin.

All four successful columns are committed together, with **H→y and V→x set to
zero**. The acquired matrix is saved and kept fixed: small correction results
cannot overwrite it. With the checkbox off, the saved matrix is reused; without
a fixed matrix, the existing fit to up to 64 recent correction observations
remains available. Cross-plane entries are zero in both modes, including when
loading older caches. The matrix, observations and fixed-mode flag are saved
atomically to `active_feedback_response.json` in the selected save folder.
Phase changes and beam loss retain the model.
The **Response matrix to use (YYYY-MM-DD HH:MM:SS)** heading shows the last
measurement time, which is saved with the matrix and preserved on reload.
Legacy caches without a measurement timestamp show **measurement time unknown**;
their file-save time is not substituted for an acquisition time.
The latest cache in the selected save folder is also loaded when the GUI opens,
before START and without connecting to the machine. The table shows the matrix
in mm/A, with its acquisition date in the title. Missing, incomplete or incompatible
caches are indicated in the title; no explanatory text or cache path appears below the table.
Opening the Active Feedback tab, changing the save folder, or pressing
**Reload saved response** refreshes this preview while idle; a running feedback
keeps displaying its own model. A valid fixed cache can be reused with startup
measurement off, but changed machine conditions may warrant remeasurement.
Previewing never modifies the cache or automatically enables response acquisition.

A stored response proposes a 0.005 A grid correction. If no identifiable matrix
is available or a proposal fails, individual +/-0.005 A candidates are tried.
Every trial is evaluated with a fresh 10-shot BPM mean after settling; improving
settings are kept and unsuccessful attempts restored. After **three consecutive
unsuccessful attempts**, return to the settings with the lowest recorded RMS
error relative to the current orbit reference during this correction episode.
The return uses steps no larger than 0.005 A. Resume timing and transmission
evaluation, then phase recovery if still degraded and enabled. Successful
attempts reset the failure count. In the adaptive fallback mode, accepted and
rejected observations both update the fit; the acquired fixed matrix does not
change. The current correction reference is used for acceptance and stopping.

The Config → Active Feedback tab exposes settling time (default **2 s** after each write), cycle interval
(default **30 s** after a complete feedback loop, before the next timing search),
local-search improvement deadband (absolute ratio, default **0.0005 / 0.05 percentage point**), and
excursions from the starting values (defaults:
timing ±112 ns, CM8L ±8°, correctors ±0.5 A). Correctors additionally require valid
IOC control limits and remain within ±5 A. Reaching a timing/phase search boundary
stops with a range message rather than treating the boundary as a local maximum.
Deadband is the minimum improvement required to accept a search step; 0.001
means 0.1 percentage point of transmission (for example, 0.800 → 0.801).
It does not change the separate 5-percentage-point transmission-degradation criterion.
Only Active Feedback allows setpoint/readback differences up to **1° for CM8L
phase and 0.1 A for correctors**, both at initialization and after writes/restores.
Timing tolerance remains 0.01 ns. Successful PUTs and finite readbacks are still
required. These tolerances accommodate coarse readbacks; command increments
remain 0.5° and 0.005 A during feedback; startup response acquisition uses larger, bounded excitations.
Active Feedback shares Main's workspace, run-button styles and status colors.
The log is available via **Show log**, leaving more space for live plots.
The English status banner shows only the current operation (timing scan, phase
scan, orbit correction or transmission evaluation).
Routine BPM checks, sample counts and settling countdowns stay in the run's
`events.jsonl` log. Only cycle waits show **Next timing scan in N s**.
BPM drift interrupts that wait immediately at the next monitoring check.
**Parameter Changes from Start** shows two live plots in place of the parameter
table: timing and CM8L phase share a plot with separate ns/degree axes, and all
four corrector currents share an A-axis plot. Both show changes from the initial
setpoints against elapsed time, including temporary probes and restorations.
Step traces extend through cycle waits and update with the other live plots.
Timing and phase use symmetric vertical limits, keeping zero at the center of
both axes as their independent ranges expand. When the post-calibration feedback
reference capture finishes, all plot histories clear and the feedback elapsed-time
axes restart at zero using the newly captured references and thresholds. The
acquired matrix and full operation logs are retained. Partial monitor batches
are discarded at this boundary so
calibration samples do not enter the first feedback monitor mean.
The transmission plot shows only **Initial**, **Threshold** and **Measured transmission**.
Threshold follows the confirmed recovery baseline minus the configured absolute
drop (default 5 percentage points). Its step trace preserves each baseline
promotion's time and extends the current threshold through cycle waits; Initial
remains fixed at the starting transmission.
During timing/phase scans, its blue **Measured transmission** trace and current numeric value
show the exact fresh 10-shot transmission used by control, without further
averaging. Background means do not replace those evaluation values.
During orbit correction, response acquisition, best-orbit restoration and cycle
waits, the same trace continues with background nonoverlapping 10-shot means;
these observations are display-only. Both calculate the downstream
ICT mean divided by the upstream mean. Repeated timestamps do not count, and a
nonpositive ICT clears the background window. The canvas redraws every 0.5 s;
timing/phase scan points are added only when a control measurement completes.
BPM control evaluations also acquire ten new distinct shots for each mean.
The BPM plot includes these evaluation means and updates on every new
nonoverlapping ten-shot monitor batch, including during timing/phase scans,
orbit correction, response acquisition and waits. No BPM moving average is used.
The degradation decision uses one such control measurement by default
(10 valid ICT shot pairs, with no additional five-measurement averaging). The
Config averaging count defaults to 1 and can be increased explicitly. The
operating-point result updates once per
completed transmission check; it and the adaptive baseline remain in the summary
and logs rather than adding curves to the plot. ICT pairing uses timestamp advancement, not pulse IDs.
RF power loss triggers automatic pause (see the shared Main/Active Feedback RF
interlock below). **ICT-triggered automatic pause is also enabled**: if LN0 or
the selected downstream ICT is zero or negative for **20 consecutive distinct
shots**, temporary probes are restored and feedback pauses. Counters are separate
for each channel, a positive shot resets its counter, and cached timestamps never
count twice. Recovery still requires manual Resume. Transmission measurements
continue to average 10 valid shots; unusable samples are excluded and remain
subject to the acquisition timeout. This does not disable the transmission degradation
threshold used to choose phase recovery. Invalid ML8L/ML10L records are logged
and retried without automatic pause. Missing data or unconfirmed writes may still
stop the worker with an error. Restore commands are permitted during RF loss;
Resume remains manual and retains the response matrix and original references.
Baseline promotions are explicit logged events. Measurements require 10 *new* BPM
timestamps or ten *new* ICT timestamps
after an acquisition barrier, with a 15-second timeout; repeated cached records
do not count. ICT pairs require both channels to advance but are not pulse-ID
matched. The BPM waveform is interpreted with the existing LINAC monitor map and
µm-to-mm conversion.

Initial references, all commands, individual samples, accepted settings, response
matrices, baseline confirmations/promotions and decisions are streamed to `events.jsonl`, alongside `config.json`,
in a timestamped `ActiveFeedback` directory under the selected LinacOpt save
location. `operations.csv` records each command request and result with a
timezone-qualified timestamp, stage, cycle, command ID, PV, previous value,
requested value, confirmed readback, restoration flag and error/status. Both
logs are flushed immediately; automatic pause, recovery and stop events appear
in the detailed log. Plots show transmission (including probes) and orbit deviations. This
is a background worker in the open GUI, not an independent service.

Offline feedback regressions: `python -m unittest Knobs.tests.test_linac_feedback`.
For a separate, prefixed loopback IOC integration test (caproto, pyepics and
libca required), run `python -m Knobs.tests.check_linac_feedback_ioc` in a fresh
process. It disables CA network discovery and uses an isolated local port.

## LinacOpt current-value display

Main current values use persistent PyEPICS CA monitors (the same mechanism as
`camonitor`), one subscription per unique preferred source PV: 21 scalar PVs.
The 3-second timer only reads the local notification cache; it does not GET or
PUT PVs or construct the machine interface. Readback PVs are used where listed,
otherwise the setpoint PV is monitored. Disconnected or not-yet-received values
show `-`; the display does not fall back to reading setpoints or showing zero.
Phase display retains 0.5-degree rounding. Closing the window releases its
subscriptions without clearing CA channels shared with the optimizer.
Subscriptions remain active while other tabs or optimization are in use;
notifications follow IOC posting rates. This reduces repeated reads, but does
not remove subscription/search/reconnection traffic. Actual server routing
still depends on the execution host's EPICS CA configuration. Optimizer
acquisition and machine control are unchanged.

## LinacOpt timing

Under the `Timing` parent checkbox, `Coarse` scans `EVE_LINAC.OT8D` as before: by default, ±10 integer counts
around its current value (11.2 ns per count).

Enable `Timing` and select only its `Fine tuning` child to scan the current timing over ±11.2 ns
in 2.8 ns steps. It measures all nine points, then reapplies and remeasures
the best valid point, regardless of the selected BO/ternary method.
`EVE_LINAC.RFDLY0` changes in increments of five counts (0.56 ns per count);
carry and borrow adjust OT8D to keep RFDLY0 within 0–19. Changes are relative
to the current value: for example, `(OT8D, RFDLY0) = (100, 18)` becomes
`(101, 3)` at +2.8 ns. Fine tuning runs immediately after Coarse, before QA/QM
and the separate corrector stage, in Sequential, Group BO and Group LBO modes.
With `Correctors & L8`, the combined group performs the coarse timing search;
Fine runs immediately after that group instead. Fine-only remains supported.
Both registers are saved for restoration.

## LinacOpt progress and plot export

The enlarged status badge shows the active axis/group's search budget, for example
`QA1L | eval=8/17`, plus `total` for the run-wide recorded evaluations.
Initial/reused and warm-start search points count toward the local budget.
Retries of a paused point keep the same local index. Final remeasurement of
the best settings is labelled `final check` and does not consume another
search slot; it does increment `total`. Completed scans restored from a
resume file show `restoring saved best` and their configured maxeval.

Below the status, **Total progress** shows completed search slots divided by
the sum of the selected groups' maxeval budgets. A group that finishes early
(including EI termination or a restored completed scan) contributes its entire
budget. Unfinished groups retain their partial progress on manual stop. Final
verification and retries do not add budget. Sequential QA/QM refinement is
included in the planned total from the start.

The local eval denominator is an upper limit for the current scan, not a promised number
of measurements or a whole-run limit. BO can stop early. QA/QM refinement
extends the displayed limit by its extra budget. Group-LBO displays the upper
bound from its iterations, directions and line samples; deduplication and
early stopping can reduce the actual count. Timing fine displays its nine-point
budget. Sequential warm starts cannot overrun maxeval while filling initial points.

On normal completion or **Save and End**, plots are saved to the usual run
directory, including the displayed 1D BO model/acquisition figure when present.
The GUI then automatically saves one combined
PNG to `/mnt/nas1/atf-data/op/tempfiles`, with a run-specific unique name. It
contains the evaluation plots and the available BO figure arranged together.
No separate plots or data files are copied to tempfiles. There is no confirmation
dialog. NAS errors are reported without deleting the normal outputs. Export
is skipped for an empty run, a failed run, or while closing the application.

**Reset To Initial** restores only the parameters selected at **START**, even if
the checkboxes have since changed. A timing-only run captures and restores only
`EVE_LINAC:OUT0:SetData`; unselected phase and magnet channels are excluded.
Selected klystron members and correctors are handled individually. Gun orbit
auto restore includes the correctors it can change, and a combined Correctors/L8
run includes its phase and timing parameters. Start snapshots read exact finite
setpoints. On exceptions, missing values, or non-finite values, parameter reads
retry the same PV at **0.5-second intervals, up to 5 attempts including the first**.
This also applies to scan starting values, corrector range checks, gun orbit
corrector readbacks, and current values read before Reset. Exhausting the retries
stops the operation instead of substituting zero or another PV. A successfully
read zero remains valid. Retry, recovery, and failure messages identify the PV
and attempt count; worker retries honor Pause/Stop at each attempt.
Reset validates all selected initial values before issuing any commands.

Each reset appends to `ResetInitial.jsonl` in its run directory and lists the
restored PVs in the GUI log. The file records timestamps, the requested parameter
list, values before reset, initial targets, confirmed setpoint values, and the
completed parameter list. A failed command stops the reset and records the
failed PV and parameters already restored; it is not reported as a complete
reset. Setpoint confirmation checks the command PV, not the physical response.

LinacOpt exports use a fixed figure size independent of window geometry.
Combined exports switch from two columns to a vertical stack for wide plots
or an excessively wide sheet, preserving each plot's aspect ratio.

## Optimizer workspace

Main and Active Feedback automatically monitor `CM0L:poutReadPeak` and
`CM1L:poutPeakRead` through `CM8L:poutPeakRead`, regardless of the selected tuning targets. Before the first
adjustment, each channel's first valid post-start shot establishes its fixed RF
power reference. Cached pre-start samples are ignored. Any subsequent single
shot at **50% or less** of that channel's reference triggers an automatic pause,
for example: **Klystron 3 RF power is too low**. Short drops remain latched even
if power recovers before the controller next checks. An unavailable baseline
blocks startup; zero initial RF power pauses instead of becoming a usable reference.

Power recovery does not resume either mode automatically. Resolve the fault and
press **Resume**; a channel still below threshold pauses again. Main retains its
settings, interrupts settling/acquisition at checkpoints, and discards averages
that span a power pause. Closing its RF warning keeps it paused. Active Feedback
restores temporary probes before pausing. Its ICT-triggered automatic pause is enabled after 20 consecutive nonpositive shots.
RF references remain fixed across Resume and response-matrix measurement and are
recaptured on a new Start. Main records reference/trip events in
`KlystronPower.jsonl`; Active Feedback uses `events.jsonl`.

LinacOpt and IPBSM place scrollable controls on the left and live plots,
progress and results on the right. Drag the divider to adjust their widths;
the divider between live and BO plots adjusts their heights. `Show log`
opens the log below the plots. Config pages scroll independently, and saved
IPBSM plots automatically form a grid based on plot count, aspect ratios and
the available width and height. All generated plots fit in the monitor without
scrolling; resizing the window or divider recalculates the grid. Machine
setpoint details are in the left controls column.

LinacOpt's **Config** contains Max eval and Correctors / BPMs selection.
**Config → Detail config** is a subtab containing scan ranges/steps, scoring weights, the Corrector BO
model, advanced options and the save location. All tabs remain browsable during
acquisition; settings are locked until the run ends. The downstream ICT selector
is beside Objective in Main → Run Control and appears when ICT is selected.

IPBSM's operator Config exposes only Average pause ratio and representative
Sigma values. Corrector controls and their Sigma/range/origin fields are hidden
in both Main and Config; loading a legacy configuration cannot select a hidden
corrector for a new GUI run. Other optimizer settings retain their configured
values internally.

## Manual Klystron membership and gun orbit compensation

Main now has individual L1–L4 checkboxes under Group 1 and L5–L8 under Group 2.
All are initially checked. Only checked members of enabled groups are scanned,
in both Sequential and Group BO (also the legacy Group LBO runner). Empty groups
are skipped. BO dimensionality and progress budgets follow the selection;
automatic preset budgets follow the member count while manually entered maxima
are preserved. With Correctors & L8, L8 remains owned by that combined optimizer.
Membership is saved in the run configuration as `kly_group_1_members` and
`kly_group_2_members`; older configurations default to all eight members.

The optional **Auto restore ML8L / ML10L orbit (≥150 µm)** checkbox under
GUN / Solenoid / L0 is initially off. It captures ten valid samples of both
BPMs' X and Y before the gun-section scan and compares fresh ten-shot means after
all three settings have been optimized. Correction runs at that stage before
Klystron/Timing/QA/QM, rather than at each optimizer candidate.

- Use `active_feedback_response.json` from the selected save folder. The cache
  is validated and columns are matched by corrector name: Active Feedback stores
  H1,V1,H2,V2, while Main uses H1,H2,V1,V2. H→y and V→x remain zero. Main does
  not probe or retrain this matrix. A missing/incomplete cache must be acquired
  through **Measure response matrix at Start** in Active Feedback before Main runs.
- A displacement of ≥0.150 mm at either BPM activates that plane. X uses
  ZH1L/ZH2L, Y uses ZV1L/ZV2L, and both use all four. The target is the fresh
  pre-scan orbit. Active planes are reassessed on each iteration.
- An SVD solve supplies 70% of the estimated correction. A response with condition
  number ≥100 is rejected. Changes are capped at 0.1 A per corrector per iteration
  and 0.5 A from the reference current, within verified IOC limits and ±5 A.
- Evaluate each trial with a fresh ten-shot mean. Keep a correction when the RMS
  over all four coordinate errors decreases; restore a rejected trial and halve
  the next correction gain. Stop successfully when every coordinate is <150 µm.
- Attempt at most **10 iterations**. If the target cannot be reached, including
  at a current limit, return to the settings with the smallest measured four-coordinate
  RMS during this restoration, including its starting point. The return respects
  the 0.1 A step cap. Record **best_rms_restored** and finish normally so Main can
  proceed to its next optimization stage.
- Invalid BPM/beam data, current readback failures (1 mA tolerance, 2 s after the
  configured settle time), and unusable response matrices still report errors.
  Stop or a failed trial restores the previous accepted setting. Gun/RF settings
  themselves are not automatically undone by compensation errors.

`GunOrbitCompensation_<run tag>.json` records the pre-scan reference, ten-shot
acquisitions, response cache path and measurement time, matrix used, iterations,
RMS results, restored best setting and termination reason. The shared response
cache is not modified. A retained correction adds one final objective measurement
to the run CSV. The four currents participate in initial-state restoration and
Save and End. Resume CSV is rejected with this option because a fresh pre-scan
reference is required. The worker uses its injected interface for BPM/current
reads and writes; offline tests use a coupled orbit model without live PV access.

## Development

Development, issues and pull requests are hosted on [GitHub](https://github.com/flight-simulator-framework/atf2-knobs). The [CERN GitLab repository](https://gitlab.cern.ch/flight-simulator/atf2-knobs) receives updates from GitHub through a pull mirror.

## Installation and startup

On a control-room computer, install the applications with the deployment script of your machine: [ATF2](https://github.com/flight-simulator-framework/machine-interfaces/blob/master/backends/atf2/atf2_deploy.md). It creates a shared virtual environment and one launcher per GUI, e.g. `./IPBSM_Opt_GUI.sh`.

For a local installation, use Python 3.11 or newer and keep the repositories in one directory. The commands below clone from GitHub; to clone from CERN GitLab instead, replace `https://github.com/flight-simulator-framework/` with `https://gitlab.cern.ch/flight-simulator/`. Building the magnet current–strength conversion library requires `gcc`, `gfortran` and `make`:

```bash
git clone https://github.com/flight-simulator-framework/machine-interfaces.git
git clone https://github.com/flight-simulator-framework/atf2-knobs.git
python3 -m venv venv
source venv/bin/activate
pip install -e machine-interfaces -e machine-interfaces/backends/atf2 -e atf2-knobs
make -C machine-interfaces/backends/atf2/machine_interfaces_atf2/libmagnet libmagnet.so CFLAG=-fPIC
cd atf2-knobs
python Knobs/GUIs/IPBSM_Opt_GUI.py
```
