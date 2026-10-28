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

LinacOpt also has an **Active Feedback** tab. It runs until Stop, with Main
optimization and reset disabled while feedback owns the actuators. Start captures
a fixed initial transmission and ML8L/ML10L orbit reference. Transmission is the
selected downstream ICT (DR by default) divided by LN0, independently of Main's
objective/weights. Absolute ICT values are used only for the nonpositive-beam
auto-pause guard. The normal feedback mode is timing tracking, with orbit recovery
taking priority:

1. Track a local transmission maximum on the relative 2.8 ns timing grid using
   `EVE_LINAC:OUT0:SetData`. Remeasure centre and neighbours every cycle; historical
   best scores are not reused. Centre measurements bracket each probe.
2. Continuously check timestamped `LINAC:monitors` records during acquisition,
   settling and cycle waits, averaging the latest three distinct records.
   If any of ML8L.x, ML10L.x, ML8L.y or ML10L.y
   differs from its initial three-record mean by at least the orbit threshold
   (100 µm by default, editable in the tab), restore the orbit with ZH1L, ZV1L,
   ZH2L and ZV2L. Every changed corrector moves by 0.01 A per step. The threshold
   triggers correction independently of transmission. Restore temporary probes
   and confirm the drift using three fresh records at the accepted settings
   before switching to correction. Correction targets the initial orbit until
   no improving grid move remains, a trial fails, or the iteration limit is reached.
3. After orbit correction, repeat timing optimization. Once timing has reached a
   local optimum and the orbit is within tolerance, measure transmission five
   times at that operating point. Phase recovery starts only when the mean of
   these latest five measurements is **below initial transmission × 0.95** by
   default. This is a relative 5% drop, not five percentage points. For example,
   an initial transmission of 0.80 gives a recovery threshold of 0.76. Each
   measurement uses three fresh ICT samples; deliberate timing/phase probe
   measurements are excluded from the recovery average. The drop percentage and
   averaging count are adjustable in the GUI. CM8L recovery uses 0.5° steps.
   An unresolved orbit blocks phase recovery, even if transmission is low.
4. Return to timing tracking and repeat. Pause retains accepted settings; Stop
   and closing the window remove temporary probes and retain accepted settings.

The initial response matrix is measured internally with 0.05 A probes. Subsequent
refresh probes and each actual correction step are 0.01 A. Probes respect the
configured/IOC limits, using the opposite direction or a smaller grid excursion
if necessary. The matrix is refreshed in all four directions when orbit
correction is triggered and updated from measured
correction results. A weighted fit uses at most 64 recent observations, giving
newer data more weight. CM8L changes invalidate the old response. Horizontal and
vertical cross-coupling is included; each orbit step evaluates all neighbouring
current-grid combinations against the learned response.

The tab exposes settling time (default **2 s** after each write), cycle interval
(default 5 s after a complete feedback loop, before the next timing search),
local-search improvement deadband (absolute ratio, default zero/off), and
excursions from the starting values (defaults:
timing ±112 ns, CM8L ±8°, correctors ±0.5 A). Correctors additionally require valid
IOC control limits and remain within ±5 A. Reaching a timing/phase search boundary
stops with a range message rather than treating the boundary as a local maximum.
Deadband is the minimum improvement required to accept a search step; 0.001
means 0.1 percentage point of transmission (for example, 0.800 → 0.801).
It does not change the separate 5% transmission-degradation criterion.
Active Feedback shares Main's workspace, run-button styles and status colors.
The log is available via **Show log**, leaving more space for live plots.
The English status banner identifies the cycle and operation, shows acquisition
and settling progress, and counts down **Next timing scan in N s** during cycle
waits. BPM drift interrupts that wait immediately at the next monitoring check.
**Parameter Changes from Start** lists the initial setting, current confirmed
setting (including probes) and signed offset for timing, CM8L and all four
correctors. The transmission plot shows the initial value, recovery threshold
and operating-point average separately.
If either LN0 or the selected downstream ICT is zero or negative, feedback
automatically pauses after restoring temporary timing, phase or corrector probes.
ICTs are checked during orbit acquisition, settling and the between-cycle wait,
as well as during transmission measurement; a single detected nonpositive sample
triggers the pause without waiting for a three-sample average. Restore commands
are permitted during beam loss. Resume is manual: if the loss persists, feedback
pauses again before further exploration. After recovery, timing tracking restarts
with the original reference and accepted settings; the interrupted search and
response model are discarded. An incomplete initial reference is acquired again
on Resume. Other invalid/missing data or unconfirmed writes stop the worker.
No fresh
baseline is silently substituted. Measurements require three *new* IOC timestamps
after an acquisition barrier, with a 15-second timeout; repeated cached records
do not count. ICT pairs require both channels to advance but are not pulse-ID
matched. The BPM waveform is interpreted with the existing LINAC monitor map and
µm-to-mm conversion.

Initial references, all commands, individual samples, accepted settings, response
matrices and decisions are streamed to `events.jsonl`, alongside `config.json`,
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
