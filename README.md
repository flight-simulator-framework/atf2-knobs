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
a fixed initial transmission and ML8L/ML10L orbit reference. Transmission is the
selected downstream ICT (DR by default) divided by LN0, independently of Main's
objective/weights. Absolute ICT values are used only for the nonpositive-beam
auto-pause guard. The normal feedback mode is timing tracking, with orbit recovery
taking priority. Run Control selects **Timing**, **CM8L phase**, and **Correctors**
(all on by default). Unchecked targets are never written; the selections are
locked from Start until Stop. All unchecked means monitoring only. If Correctors
is off, timing can still track, but phase recovery remains blocked while the orbit
is outside tolerance. Timing off skips timing searches; phase recovery still uses
the transmission threshold at the current operating point. All Feedback Settings
are under **Config → Active Feedback** and remain locked during a feedback run.
With all targets enabled:

1. Track a local transmission maximum on the relative 2.8 ns timing grid using
   `EVE_LINAC:OUT0:SetData`. Remeasure centre and neighbours every cycle; historical
   best scores are not reused. Centre measurements bracket each probe.
2. Continuously check timestamped `LINAC:monitors` records during acquisition,
   settling and cycle waits, averaging the latest **10 distinct valid shots**.
   If any of ML8L.x, ML10L.x, ML8L.y or ML10L.y
   differs from its initial 10-shot mean by at least the orbit threshold
   (100 µm by default, editable in the tab), restore the orbit with ZH1L, ZV1L,
   ZH2L and ZV2L. Every changed corrector moves by 0.01 A per step. The threshold
   triggers correction independently of transmission. Restore temporary probes
   and confirm the drift using 10 fresh shots at the accepted settings
   before switching to correction. Correction targets the initial orbit until
   no improving grid move remains, a trial fails, or the iteration limit is reached.
3. After orbit correction, repeat timing optimization. Once timing has reached a
   local optimum and the orbit is within tolerance, measure transmission five
   times at that operating point. Phase recovery starts only when the mean of
   these latest five measurements is **below recovery baseline − 0.05** by
   default. This is an absolute drop of **5 percentage points**. For example,
   a baseline transmission of 0.80 gives a recovery threshold of 0.75. Each
   measurement uses ten fresh ICT samples; deliberate timing/phase probe
   measurements are excluded from the recovery average. The drop in percentage points and
   averaging count are adjustable in the GUI. CM8L recovery uses 0.5° steps.
   An unresolved orbit blocks phase recovery, even if transmission is low.
4. Return to timing tracking and repeat. Pause retains accepted settings; Stop
   and closing the window remove temporary probes and retain accepted settings.

The recovery baseline starts at the initial transmission. It rises only after
improvement is confirmed for **120 seconds and at least 3 complete operating-point
means**, with timing at a local optimum and normal BPM readings. The new baseline
is the lowest confirmed mean during that interval, avoiding a transient maximum.
The GUI's **Raise baseline after [s]** controls the duration. A confirmed mean at
or below the current baseline, orbit recovery, Pause or beam loss cancels pending
confirmation. The baseline never moves down; for example, once it reaches 0.90,
phase recovery uses 0.85. The initial transmission, orbit reference and parameter
origins remain fixed throughout the run. A new START captures new initial references.

Response learning uses **only actual correction attempts**; there is no separate
calibration stage, 0.05 A excitation or mandatory four-corrector probe sequence.
A stored response matrix proposes a 0.01 A grid correction directly. If no
identifiable matrix is available, individual +/-0.01 A correction candidates are
tried; an improving setting is kept immediately, and an unsuccessful attempt is
restored. A rejected model proposal can likewise fall back to individual
corrections. Every attempt respects the configured/IOC current limits.
Each result is evaluated with fresh 10-shot BPM means after settling. Accepted
and rejected correction results both update the response. A weighted fit uses
at most 64 recent current/orbit changes, giving newer data more weight. A full
4×4 matrix requires four independent observed current directions; until then,
correction uses the direct candidates without adding dedicated learning moves.
The matrix and observations, including incomplete observations, are retained
and saved atomically to `active_feedback_response.json` in the selected save
folder. New workers load this file. Phase changes and beam loss retain the model;
subsequent correction results update it without a recalibration sequence. Invalid
cache files are logged and fall back to direct corrections. A learned matrix
includes horizontal/vertical cross-coupling and ranks neighbouring current-grid
combinations. The existing correction acceptance and stopping criteria remain
based on the orbit error relative to the initial reference.

The Config → Active Feedback tab exposes settling time (default **2 s** after each write), cycle interval
(default **30 s** after a complete feedback loop, before the next timing search),
local-search improvement deadband (absolute ratio, default **0.01 / 1 percentage point**), and
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
remain 0.5° and 0.01 A, and observations determine whether a move is beneficial.
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
The transmission plot shows only **Initial**, **Threshold** and **Measured transmission**.
Its blue **Measured transmission** trace is the ratio of the latest
ten downstream ICT readings' mean to the ten upstream readings' mean. It
updates when both channels have new timestamps, including during settling, BPM
acquisition and cycle waits; the canvas redraws every 0.5 s. Repeated timestamps
do not count, and a nonpositive ICT clears the display window. This display-only
window never feeds optimization or degradation decisions: those still use fresh
post-settling samples. The operating-point mean averages five such control
measurements by default (normally 50 valid ICT shot pairs) and updates once per
completed transmission check; it and the adaptive baseline remain in the summary
and logs rather than adding curves to the plot. ICT pairing uses timestamp advancement, not pulse IDs.
Only ICT loss triggers automatic pause: if either LN0 or the selected downstream
ICT is zero or negative for **10 consecutive distinct shots**, feedback
automatically pauses after restoring temporary timing, phase or corrector probes.
ICTs are checked during orbit acquisition, settling and the between-cycle wait,
as well as during transmission measurement. ICT counters are independent per
channel. Normal readings reset
the corresponding counter, and repeated cached timestamps never count again.
During confirmation, new commands wait; invalid readings are excluded from
averages. Invalid ML8L/ML10L records are logged and retried without automatic pause
or manual Resume; BPM monitoring refills its 10-shot window after recovery.
The existing acquisition timeout still applies when valid data remain unavailable.
Restore commands
are permitted during beam loss. Resume is manual: if the loss persists, feedback
pauses again before further exploration. After recovery, timing tracking restarts
with the original reference and accepted settings; the interrupted search is
discarded and the response model is retained for updates from subsequent corrections. An incomplete initial reference is acquired again
on Resume. Other invalid/missing data or unconfirmed writes stop the worker.
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

LinacOpt exports use a fixed figure size independent of window geometry.
Combined exports switch from two columns to a vertical stack for wide plots
or an excessively wide sheet, preserving each plot's aspect ratio.

## Optimizer workspace

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

The optional **Auto restore ML8L / ML10L orbit (≥100 µm)** checkbox under
GUN / Solenoid / L0 is initially off. It captures three valid samples of both
BPMs' X and Y before the gun-section scan and compares three-sample means after
all three settings have been optimized. Correction runs once at that stage,
before Klystron/Timing/QA/QM, rather than at each optimizer candidate.

- A displacement of ≥0.100 mm at either BPM activates that plane. X uses
  ZH1L/ZH2L only; Y uses ZV1L/ZV2L only; XY uses all four, including measured
  cross-plane response. The target is the pre-scan orbit, not zero position.
- Each iteration probes selected correctors by ±0.05 A around the current
  accepted setting, shortened at verified limits. Each probe measurement uses
  three BPM samples, and the probe is restored before moving on.
- An SVD solve of the measured response supplies 70% of the estimated correction.
  A response with condition number ≥100 is rejected. Changes are capped at
  0.1 A per corrector per iteration and 0.5 A from the reference current, within
  verified IOC limits and the existing ±5 A steer envelope.
- A correction is kept only if the selected-plane residual norm decreases and
  the unselected plane stays below 100 µm. Completion requires all four BPM
  coordinates within 100 µm of the reference. There are at most five iterations.
- Invalid/missing BPM samples, bad beam status/charge, failed current readback
  (1 mA tolerance, 2 s after the configured settle time), singular response,
  limits or failure to converge stop the run. Temporary probes and rejected
  steps are restored; the last accepted correction remains. Gun/RF settings
  themselves are not automatically undone on compensation failure. User pause
  takes effect at a boundary with temporary probes restored; stop also restores
  a pending probe or unaccepted trial.

`GunOrbitCompensation_<run tag>.json` records the reference, three-sample
acquisitions, tested currents, response matrices, iterations, residuals and
termination reason. Response probes do not count toward BO max eval and are not
training points. Successful correction adds one final objective measurement to
the run CSV. The four currents participate in initial-state restoration and
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
