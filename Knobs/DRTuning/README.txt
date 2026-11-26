DRTuning — RFTrack / machine interface launcher
==============================================

Temporary DeltaFRamp frequency-readback bypass (2026-11-19)
---------------------------------------------------------
At the operator's request, the bundled default/example profiles now set
rf.verify_frequency_readback=false. Restart DRTuning to load the change.
Custom site profiles can select the same setting; omission retains verification.
With this setting, RF snapshots, switching, restoration and stale-setting
comparisons do not read/process FREQUENCY:DR_read or require its agreement with
the requested offset. RAMP ON/OFF, HOLD and selected-preset checks, settling
waits and failure restoration still run. Logs explicitly say the frequency
check was skipped; snapshots record frequency_readback_verified=false without
inventing an RF readback. Dispersion uses the requested preset frequency span.
Set rf.verify_frequency_readback=true to restore the physical-frequency checks.

Vertical dispersion and RF power pause
--------------------------------------
Current optics (no tune fit) is the default for ZV selection. Find ZVs imports
the magnet settings and checks closed-orbit stability before calculating ZV
responses in RFTrack. This mode works without a tune reading. An unstable
model stops selection before the RF measurement.
Choose Current optics + tune fit when the configured tune targets are known
to represent the current operating point. The default fit allows ±5% change
in model QF1R/QF2R strength with a tolerance of 1e-5 in each fractional tune.
Saved nominal RFTrack uses the bundled 4 kHz reference matrix.
For mapped-channel differences use RFTrack model > Exclude unmapped BPMs /
magnets. Measure selected ZVs measures the actual response before Apply.

Vertical dispersion uses ZV1R–ZV8R, ZV17R–ZV32R and ZV43R–ZV51R in
both RFTrack selection and full measured mode. Operator exclusions also apply.
Choose Response measurement > Full measured ZV matrix, then Measure all enabled
ZVs to measure every eligible column and fit the correction with bounded SVD.
Review the proposal and use Apply / remeasure. Recalculate reuses the matrix.
After Apply it uses the measured after-Apply orbit, dispersion and currents for
the next proposal. Each Apply still checks the machine against that baseline.
Save data + plots and Select ZV matrix also support full measured scans.

Vertical dispersion ranks ZVs in RFTrack mode and fits responses with a weight of 1
on vertical dispersion error and 0.05 on the change from the measured vertical
orbit at nominal RF. Set Maximum ZVs (default 5) and click Find ZVs.
Measure selected ZVs measures only those response columns. Review the proposed
currents and predicted curves, then click Apply / remeasure to see the measured
after curves. Select BPMs / magnets and Recalculate reuse the measured columns.
The full and shortlisted ZV measurements automatically save their matrix JSON.
Use Select ZV matrix to open the timestamped
vertical_dispersion JSON (older saves use dispersion.json). JSON, CSV, PNG
and PDF share the same filename stem. The Vertical Dispersion save and load
dialogs open in /mnt/nas1/atf-data/flight-simulator/DRTuning/<year>/ each time.
Select ZV matrix imports the measured ZV columns. The label shows their acquisition
time. Legacy files with no acquisition timestamp show "measurement time unknown".
Both shortlisted and full ZV scans accept a measured vertical dispersion column
at S/N >= 5 by default. The response is the RMS change in vertical dispersion
over common enabled BPMs. The noise combines the measured endpoint standard
errors at both RF offsets, using their actual current spans.
Measure selected ZVs and QM CALC group probes by RF setting. At nominal RF
measure the baseline orbit and all low/high current pairs, then repeat at the
negative and positive RF offsets. Each probe moves one magnet and restores it
before the next magnet. Each RF block starts and ends with a reference orbit
at the original magnet settings. The JSON stores both reference frames and
the RMS change in X and Y. The solver uses the measured current span at each RF.
One successful pass takes three RF changes including return to the initial
RF state. Candidate selection and post-Apply measurements are separate.
During dispersion baseline acquisition and after Apply, a newly invalid enabled
BPM triggers one retry of the BPM batch after 3 s at the same RF. The second
batch supplies that RF point, and persistent invalidity follows the existing
BPM masking or Apply checks. Grouped QM and ZV reference orbits also retry.
The grouped ZV scan waits three seconds and repeats the same BPM acquisition
once if common coverage is lost at a probe endpoint. Persistent loss during
the initial width search restores the ZV and retries it at a smaller width.
Update dispersion measures the current dispersion and COD at three RF settings,
even before selecting a matrix. Recalculate reads the present magnet state and
limits, then fits the latest observation. It measures dispersion if no current
observation exists or if the settings have changed. Apply remains a separate step.
The vertical weights default to 1 for dispersion and 0.05 for COD change.
Vertical dispersion uses MB1–18, MB32–64 and MB81–96 (inclusive). Both vertical
and QM horizontal correction weight COD changes only at MB65,66,19,22,23,24.
Reference BPMs outside these regions are shown faintly on the respective plots.
Choose Horizontal (QM) or Vertical (ZV) under Dispersion Correction.
Drag the divider between the dispersion plots and the results table to resize
them. On narrow windows, use Controls and Plots and results to switch views.
Bump scan is a top-level entry. Other tools are grouped under In development.
During bump calibration, shared matrix measurement or an XSR scan, Pause bump
scan interrupts acquisition and requests restoration of the temporary currents.
The pause dialog offers Resume and Save and end. Resume retains completed XSR
points and the target queue. An incomplete XSR point is measured again.
An interrupted calibration restarts for the current target.
Both response scans show the RF offset and block, magnet index within the block,
low/high current command and readback, and BPM samples at the current setting.
The column counter advances after checking the response across RF settings and
includes skipped magnets. Adaptive retries show their reason and probe width.
Completed dispersion measurements, matrix scans, proposals and Apply results
export a timestamped PNG to /mnt/nas1/atf-data/op/tempfiles/. QM filenames
include trim or main. JSON data and checkpoints use the normal data directory.
The plots compare dispersion RMS before correction, as predicted, and measured
after Apply on the same valid, included BPMs. Vertical uses the target residual.
QM reports straight BPMs and, when weighted, the selected arc parity separately.
The absolute and percentage changes refer to the pre-correction measurement.
On either plot, hover over a measured point to read its BPM name, position
and signal value. Click the point to exclude it or the grey point to include
it again. The curves join the remaining points across operator exclusions.
The toolbar pan and zoom tools temporarily take precedence over selection.
During a scan, the plot stays interactive after the baseline arrives. New
probe evaluations and the final fit use the current selection. An Apply
already in progress retains its proposed magnet settings. After editing a
finished measurement, Recalculate before Apply (Find ZVs again for a model
shortlist). Save BPM selection in the plot toolbar overwrites the defaults
for this user and interface. The shared BPM / magnet selection shows the same
BPM mask, including in COD and skew correction.

On the Machine interface, each measurement or correction worker monitors klystrons 0–8 starting at
its first machine access. A reading at or below 50% of that klystron's initial
positive power pauses the worker at a machine I/O checkpoint. The popup
identifies the klystron. The worker resumes automatically once all nine powers
stay above threshold for three seconds. Closing the popup dismisses the notice. Interrupted BPM
and XSR sample groups are collected again. Cancel / restore follows the active
operation's restoration procedure. If initial RF readings are unavailable for
15 seconds, the operation reports an error.

Measured dispersion and local-bump / XSR correction (2026-11-19)
---------------------------------------------------------------
SysID is unchanged. DRTuning LiveBackend waits for advancing EPICS timestamps:
DR:monitors for COD and XSR:PROJ_V_SIGMA for vertical size. It parses the gated
payload without re-reading it. The new correction tools acquire 10 updates per
setting, save all values/timestamps, and reject missing/invalid beam data.
The existing correction panel defaults to 10; its count remains editable.
update_timeout_s in the site profile defaults to 15 s per update. Freshness
means a new PV update, not proof of independent beam shots or camera frames.
XSR _AVE and SIGMA_SIGMA are not used. Size units follow the existing source
plane XSR calibration in micrometres. A configurable post-current settling
delay precedes the acquisition watermark; IOC processing latency still matters.

QM dispersion:
  Use QM dispersion > Measure response / CALC, inspect the current table and
  before/predicted curves, then Apply / remeasure dispersion. Candidates are
  QM1R–QM4R and QM20R–QM23R, both .1/.2 (16 individually mapped trims, including
  QM2). QM2R.1, QM2R.2 and QM3R.2 are excluded by default (13 active candidates); the
  Exclude QMs field lists these trims. Select QM coil beside Solver to switch
  from Trim (default) to Main. Main starts with all eight shared supplies,
  including QM2R. CALC measures one response per main supply. Apply and Restore
  use the same coil as the scan. Exclusions are independent for Trim and Main.
  On live QM main supplies, setting and restoring a current accepts a readback
  within 1% of the absolute target, with the configured absolute tolerance
  (default 0.02 A) as the minimum. At 100 A the accepted range is 99 to 101 A.
  Older scans load as Trim.
  Trim defaults: per-QM initial widths inferred from the 2026-11-19 13:58 scan,
  targeting S/N 6.5 (30% above acceptance threshold 5), in A:
                 QM1R QM3R QM4R QM20R QM21R QM22R QM23R
             .1  .16  .34  .13   .62   .50   .28   .16
             .2  .22  .21  .18   .47   .28   .20   .29
  No checkpoint load is required. The GUI always uses per-QM starting widths.
  These are widths around the current allowed reference, not absolute currents.
  The fallback width is 0.1 A, used for a QM without an individual width
  (all main supplies on the first scan, and QM2R trims if enabled). It is editable in Measurement.
  Maximum current change per Apply defaults to 3 A and accepts larger values. RF full span is 4 kHz
  (baseline +/-2 kHz). Absolute currents are bounded by the supply limits.
  The existing confirmed site mappings, coil calibration, current limits
  and readback checks are used. Explicit exclusions are available in the panel.
  CALC measures a two-current response, normalizing by actual readback span.
  Probe and Apply both respect parameter.A (lower) / parameter.B (upper).
  There is no additional absolute +/-3 A cap. Parameter PVs take precedence
  over static limits when both are configured; limits are reread before each
  pair and validated again by the live backend before each write.
  Each endpoint is automatically clipped, independently. At a 0 A lower limit,
  a requested [-.01,+.01] A pair becomes [0,+.01] A. Near a limit the pair may
  be asymmetric; at a limit it is one-sided. The actual measured current span,
  not twice the nominal probe width, is always used in the response derivative.
  Probe widths and commanded probe endpoints use a .01 A grid; narrower ranges
  are skipped. The fallback width is editable, with .01 A as the minimum.
  If the baseline is outside the probe cap but inside the supply range, the
  probe reference is moved to the nearest permitted boundary. For example,
  baseline 3.2 A with a +/-3 A cap and a .01 A width uses [2.99,3.00] A, then restores
  the original 3.2 A. The recorded current pair and scheme identify this shift;
  its response was measured away from the original operating point and may
  differ if the optics are nonlinear. Actual dispersion is verified after Apply.
  If the ranges do not overlap, the span is unresolved, or the original current
  cannot be restored within supply limits, skip that QM without commanding it
  and continue with the others. Skipped QMs are always omitted from the solver,
  even in fixed-probe mode, and are not written by Apply or Restore. The UI/log
  records the name and reason. Checkpoints retain skipped status; zero column
  placeholders are explicitly marked unmeasured by response_column_measured,
  with null quality metrics rather than invented zero-signal measurements.
  If every QM is skipped, stop without a correction proposal. Nonfinite/reversed
  supply limits still fail validation instead of being treated as a usable range.
  Max change / Apply [A] controls |delta_I| for each correction (step_A, default
  3 A), independently of probe width. Legacy probe_current_limit_A fields are
  accepted when loading saved files but no longer impose an absolute current cap.
  The QM GUI has no checkboxes: per-QM starting widths, adaptive probing and
  reuse of matching measured estimates are always enabled. Tune acquisition
  and COD hard constraints remain disabled. Measurement and Correction groups
  contain the relevant numeric settings; CALC and Apply are followed by BPM
  selection/recalculation, Load/Resume and Restore. Data shows the save folder.
  Start each QM at its preset, remeasure at a rescaled width in the same CALC
  if S/N is insufficient, and shrink/retry invalid probes. Matching estimates
  from subsequent scans take priority over presets. S/N<5 columns are not used
  in correction. Low-quality saved scans show that CALC must remeasure before
  Apply is possible. Legacy first-pass and fixed-probe options remain readable
  in checkpoints and available to programmatic callers, but have no GUI toggle.
  Later scans reuse estimates when machine/profile, baseline settings, RF
  options, fallback setting and BPM exclusions match. Estimates remain in the
  session and checkpoints; loading a checkpoint restores them. The table shows
  Next probe [A].
  For a weak response, estimate the next span from target/measured S/N with 30%
  headroom, convert it to the necessary width including clipped/one-sided
  geometry, and round UP to .01 A. Growth is capped at 3x per trial by default,
  as well as the allowed current range. S/N>=5 is accepted immediately; a strong
  response is not remeasured at smaller width during that scan. Instead its
  estimate can reduce the starting width of the next scan. Zero observed noise
  cannot establish a smaller target width, so the measured width is retained.
  The target uses RMS uncertainty
  from valid-sample SEM of the four RF +/- orbit means in the QM current pair.
  Log/table values convert the dispersion change to an RF orbit difference in
  micrometres: target = 5 * measured uncertainty, not a fixed displacement.
  This estimate does not account for slow drift, systematic errors or correlated
  PV updates. Zero observed scatter is shown explicitly, not as a finite S/N.
  New invalid BPMs (including loss of all common straight BPMs) restore the
  original current, halve the width and round DOWN to .01 A: .05 -> .02 -> .01 A;
  from the default 3 A: 3 -> 1.50 -> .75 -> .37 -> .18 -> .09 -> .04 -> .02 -> .01 A.
  The failed width is a strict upper bound for later growth during that QM scan.
  At .01 A, or the 12-trial limit, skip an invalid QM and continue. Failed
  measurements remain in diagnostics but do not remove BPMs from accepted
  columns. Cancellation, RF/tune/PV errors and restoration failures do not retry.
  At the width/attempt limit, weak responses are retained in
  the record but omitted from the adaptive-mode correction solver. If none are
  usable, no correction proposal is produced. S/N is adjustable in the panel.
  Each dispersion observation measures nominal COD plus symmetric RF offsets,
  with 10 BPM updates at each setting; RF and temporary currents are restored.
  With one pair per default 13 active trims, three RF blocks acquire
  3 x (2 reference orbits + 26 probe orbits) x 10 BPM updates = 840 updates.
  A failed BPM coverage check retries the same RF/current point once after 3 s.
  If the probe must shrink, all three offsets are checked at the new width for
  that QM. For S/N-only growth, the accepted nominal COD slope is reused and
  only the two off-energy pairs are acquired when nominal COD is resolved at
  the S/N threshold and the COD limit is disabled. ZV scans use the same rule
  with vertical COD. The retry starts at the current RF side and crosses once
  to the opposite side. Otherwise nominal COD is also measured at the new width.
  The actual current endpoints for each RF are recorded in actual_by_rf.
  Magnet settling and BPM averaging still contribute to total acquisition time.
  "Lost all common valid straight-section BPMs" means no enabled straight BPM
  remained usable through every accepted observation, not necessarily that all
  BPMs failed simultaneously. QM dispersion excludes a BPM after FIVE consecutive
  invalid updates. Valid updates require finite X/Y, positive finite intensity
  and accepted BPM status. A good update resets the streak; consecutive failures
  carry across RF and QM settings within CALC or Apply. Starting a new CALC or
  Apply resets streaks. The common mask is intersected across all QM probes.
  Each RF setting still acquires 10 updates. Means and standard deviations use
  only valid samples; missing/invalid values are never averaged or filled in.
  Thus an accepted average can contain fewer than 10 samples. Saved data include
  valid_sample_count, max_invalid_streak, trailing_invalid_streak and the limit.
  Other correction tools retain their previous sampling/validity rules.
  For grouped scans, rf_blocks links each reference and probe to the raw
  orbit_measurements entry, including the RF setting, current readback, BPM
  names, sample counts and status diagnostics. probe_attempts records rejected
  trials and their next widths.
  Once excluded during a scan, a BPM stays out of that scan's common response.
  Dx is targeted to zero at legacy straight BPMs MB19R–MB31R / MB66R–MB80R.
  QM weights default to 1 for dispersion, 0.1 for COD change and 0 for arc
  horizontal dispersion. A positive arc weight targets zero horizontal
  dispersion at odd (default) or even MB numbers in MB1–18, MB32–65 and
  MB81–96. Remaining arc Dx and all Dy target their measured baselines.
  Invalid and excluded BPMs are omitted before computing the RMS. The arc
  row multiplier is w_arc * sqrt(N_straight/N_arc), giving equal straight
  and arc weights equal influence for equal RMS errors. Recalculate uses
  the stored matrix, and Apply verifies the weighted measured dispersion
  improvement. The panel shows the selected arc BPMs in the parity tooltip.
  The default uses all enabled trims in bounded regularized SVD; Greedy chooses
  the best response columns up to the selected maximum. At the operator's
  request, the COD hard limit is OFF by default for probing, solving and SET.
  COD remains a weighted objective targeting its measured baseline. Enabling
  Enforce max COD change restores the .05 mm maximum in both planes (not RMS),
  including probe checks and measured post-SET rollback on excess.
  This extra .05 mm guard is not present in legacy drhdispbyq.n; coddispersion.n
  includes COD as a weighted fit objective instead. Its factor1=.05 is a
  dispersion weight, not a .05 mm COD limit. Appropriate tolerance depends on
  measured BPM noise/drift and the acceptable orbit change.
  Relative regularization alpha=.01 means lambda=alpha*s_max, where s_max is
  the largest singular value of the weighted response of the selected QMs.
  The solver adds lambda^2*sum(delta_I^2) to its retained-mode squared residual.
  It is not a 1% current limit. Increasing alpha suppresses weak modes more;
  alpha=0 removes that penalty but retains the separate SVD cutoff of .04.
  At the operator's request, tune acquisition and tune constraints are OFF by
  default. CALC and SET do not validate, wait for or read tune PVs in this mode.
  The UI/log/data state that tunes are skipped; missing tune values, responses
  and predictions are stored as null, not as zero tune changes. To re-enable,
  check Measure / constrain H and V tunes, then run a fresh CALC. Tune-enabled
  operation constrains |delta tune| <= .002 by default. Q-modulation beta scans
  keep their separate tune measurement requirements.
  Apply gain is 1: I_set = I_baseline + proposed delta_I. The solver already
  bounds |delta_I| by the per-Apply limit and I_set by the supply limits. SET verifies
  actual dispersion improvement, and COD/tunes only when their constraints
  are enabled. Failed measurements or enabled checks restore changed trims. The graph
  shows measured before, predicted, target, and measured after, for Dx/Dy/X/Y.
  To exclude a suspect BPM after CALC: BPM selection > uncheck it > OK >
  Recalculate (measured response). This uses the measured baseline and response
  without any machine reads/writes or fresh BPM acquisition, updates the plots
  and proposed currents, and requires a separate Apply. Selection changes
  disable the old Apply proposal. Acquisition validity and original scan options
  are retained separately: rechecking a BPM never revives invalid scan data.
  Changing RF span, momentum compaction or QM selection requires a new CALC;
  enabling tune constraints requires a measured tune response and, after Apply,
  verified after-Apply tunes. After a successful Apply, use Recalculate > Apply
  to iterate without scanning the response again. Recalculate uses the measured
  after-Apply dispersion / COD and verified QM currents as the new baseline,
  retaining the response, acquisition validity and prior iterations in saved data.
  It performs no BPM acquisition or magnet / RF writes. Apply checks live settings
  against this new baseline, limits each step to |delta_I| <= step_A while keeping
  absolute currents within parameter A/B, and measures the result again.
  Repeated corrections may exceed 3 A in total. Failed Apply restores the preceding
  iteration's currents. Restore original QM / RF returns to the initial scan
  settings, including after preparing the next iteration. A response reused after
  optics changes is an approximation; use CALC again if prediction and measurement
  diverge. Next probe [A] is for a subsequent CALC at matching settings, not Apply.
  QM filenames include the response coil (trim or main) after qm_scan or
  qm_dispersion for checkpoints, applied JSON/PNG and manual save suggestions.
  CALC automatically writes qm_scan_*.checkpoint.json under /atf/data/flight-simulator/DRTuning/<year>/
  (or the chosen save folder). Each complete RF block is checkpointed, followed
  by the accepted columns and new probe widths when that pass completes.
  Writes use a temporary file, flush/fsync, and atomic replacement so an
  interrupted write preserves the previous checkpoint. Saving failure stops
  the scan after restoration; it does not continue accumulating unsaved pairs.
  Resume unfinished QM scan uses the current partial result, or first choose
  Load QM scan checkpoint after restarting the GUI. Completed response columns
  and complete RF blocks of the pending pass are reused. On a handled stop,
  the checkpoint also retains restored pairs in the interrupted RF block.
  The unfinished pair is repeated after the resume baseline check. Changing the probe definition replans the pending magnets. Loading a scan never overwrites the
  current initial-width control.
  Resume checks machine identity/profile, BPM order, acquisition RF/tune
  settings, QM selection, supply limits and original machine settings before
  any probe. It remeasures the baseline (30 BPM updates), resets invalid streaks,
  and uses that current baseline as the fit target while retaining prior columns.
  Baseline history and interrupted frames remain in the saved data. GUI resume
  creates a new checkpoint file, preserving the loaded original.
  If a crash left a QM or RF offset applied, Resume refuses to move anything.
  Restore original QM / RF settings explicitly restores those scan settings;
  any changed correctors/skews must separately match the saved baseline.
  To use a completed QM response matrix at a different operating point, use
  Select matrix. Both trim and main files are recognized; the panel identifies
  the coil and acquisition time. Update dispersion measures only the current
  dispersion and COD (10 BPM updates at each of three RF settings). Recalculate
  reads current QM settings and parameter A/B. It acquires an observation when
  needed, or reuses the latest one taken at matching settings. Review Proposed
  [A] before Apply. Restore original QM / RF refers to the current iteration series.
  Automatic scan resume does not resume or automatically perform Apply.
  Old files without committed scan state cannot recover partial responses.
  Successful Apply automatically saves JSON and PNG to /atf/data/flight-simulator/DRTuning/<year>/,
  matching the LinacOpt output-root/year convention, with unique timestamped
  names and the actual before/after measurements,
  fit options, original acquisition options, response and applied currents.
  Change folder changes the destination for this session;
  the saved path is shown in the panel and log. Saving never changes hardware:
  a file error leaves SET applied and reports that manual saving is needed.
  Save data + plot remains available for manual saving, including failed SET
  measurements. Failed SET attempts are not automatically saved.

  When tune acquisition is enabled, "No fresh H/V tune pair" means the PV tune
  mode could not acquire TBT:TUNE:H and TBT:TUNE:V;
  it does not start TuneDR acquisition. COD BPM updates do not advance these
  tune PVs. Check the op-base TuneDR reader/acquisition and H/V FFT peak results.
  Both timestamps must advance, their difference must be <= .5 s, and both
  fractional tunes must be finite and between 0 and 1 (default timeout 10 s).
  Timeout diagnostics now show each PV value, initial/latest timestamp, update
  and validity flags, and the H/V time difference. This distinguishes stopped
  acquisition from invalid FFT results or mismatched update times. Stale tunes
  are not treated as zero tune response: QM tune constraints require measured
  changes. The temporary DeltaFRamp readback bypass does not bypass this check.

Vertical dispersion:
  Continue to use Dispersion > V with vertical correctors. Response / limits /
  SVD settings now offers the existing SAD-style greedy solver and an all-
  enabled bounded regularized SVD solver. The latter ignores maximum-corrector
  count and solves within absolute and step limits, without post-fit clipping.
  Neither algorithm is universally best: greedy uses fewer supplies; the full
  solve distributes correction and regularizes weak modes. Compare measured
  after-SET dispersion and COD, not only the predicted residual.

Bump / XSR coupling:
  The "Current operation" panel above the plots shows calibration stages, active
  corrector and baseline/command/readback currents, settling, actual BPM update
  counts, target Y, closure iteration, and outside COD max/RMS versus limits.
  Stage transitions also appear in the detailed log. The BPM counter advances
  only after each acquired update; it is not an estimated timer. Restoration
  commands and readback verification are displayed separately. Completion is
  shown only after verification; cancellation/failure retains the last stage
  and reports restoration status. XSR update counts appear during the scan.
  Choose a bump position to use its saved BPM/corrector combination directly.
  There is no Select BPMs / correctors step and no runtime RFTrack model
  preparation, lattice matching or QF tune matching in the Bump path.
  The 68 presets bundle the nominal 2026-11-19 audit's device names and
  internal-BPM transport in bump_presets.json, plus nominal response arrays
  in bump_preset_responses.npz. Saved currents/limits are never applied.
  Target mm is inferred from saved nominal transport; it is not a direct
  sextupole position measurement. Actual corrector responses are measured
  during every calibration, and actual COD closure is required before scanning.
  "Check BPMs / current limits" optionally refreshes availability for all
  positions without magnet writes. Missing/invalid required BPMs, insufficient
  outside BPMs, excluded/missing correctors and inadequate current budget make
  a position gray and unselectable, with the reason in the table and tooltip.
  Each target starts with its preset. If measured outside COD cannot be closed,
  the scan can add up to two correctors, one at a time. Candidates come from
  other presets outside the internal BPM interval. Max and RMS limits apply
  per plane. Horizontal violations use ZH and vertical violations use ZV.
  If both exceed a limit, the larger ratio to its limit takes priority.
  Candidates are ranked against the leakage in their own plane using the
  shared measured response when available, otherwise the nominal response.
  The enlarged set is calibrated on beam and both signs must close. During
  XSR optimization the previously selected window, displacement reference and
  completed XSR points carry forward. The scan continues at the unfinished
  amplitude with a fresh closure check. Each point records its correctors.
  Resume also retains the points if the extra corrector calibration is interrupted.
  If two added correctors still cannot close the bump, a queued run verifies
  restoration to that target's baseline, archives its partial data and reason
  in bump_skipped_<target>_<timestamp>.json, then advances to the next target.
  Readback, acquisition and restoration failures pause for operator recovery.
  Eligibility uses current readbacks, parameter A/B and max change from
  baseline, with nominal response only to estimate the reachable interval.
  A shifted interval with the minimum full width (default 0.2 mm) is sufficient;
  the bidirectional calibration probe must also fit. Measured closure still
  determines the actual scan interval. Recheck after machine settings change.
  ZV16R and ZH15R start unchecked in Utilities > BPM / magnet selection.
  This shared selection also applies to bump calibration, closure helper selection
  and shared response measurements. Check either corrector to enable it.
  Opening the live bump panel ranks targets by absolute trim current and checks
  the first five with available readbacks. Run checked in order calibrates
  and scans each checked target with the previous target's optimum in place.
  For many targets, Measure all ZH/ZV measures each enabled corrector with
  symmetric probes, ten fresh BPM updates per polarity, and a verified restore.
  Supplies without symmetric current margin are listed as skipped. The result
  is saved in flight-simulator/DRTuning/<year>/bump_response_<timestamp>.json
  under atf-data. Measure or Load matrix enables Use matrix with current beam for single
  and queued bumps, including extra closure correctors. Each target keeps a
  fresh baseline and verifies closure in both signs. Missing columns or invalid
  required BPM rows trigger fresh probes for the affected correctors only.
  File loading checks machine identity, BPM order and measured orbit columns.
  Calibration uses present corrector currents, limits and orbit, and verifies
  present optics during calibration. Clear Use matrix with current beam to
  measure each target independently. Bump orbit columns use achieved current
  spans as in both dispersion tools and require no RF changes.
  1. Calibrate and close bump automatically checks the selected preset against
     current BPMs and limits, then measures X/Y corrector response columns with
     10-update BPM averages. At each setting, a sample with an invalid required
     BPM is discarded and replaced at the same currents, up to three extra
     updates. Widespread invalid readings wait three seconds before retrying.
     A fourth invalid sample stops the measurement and reports the
     affected BPMs. Both signs are tested and baseline currents are
     restored. Default max current change from each target's baseline is 2 A,
     outside max 30 µm and outside RMS 20 µm. Calibration uses +/-0.1 mm.
     A BPM whose X and Y both stay exactly constant for ten timestamp-verified
     updates is temporarily excluded, even when intensity changes. Its name
     appears in Current operation. A complete valid batch with varying position
     restores the BPM; a mixed batch starting with the frozen value is discarded
     for that channel. Frozen rows do not enter COD, response fits or closure
     helper ranking. Recovered rows require a valid baseline for COD and valid
     probe data for the response fit. Missing references are filled at baseline
     currents. If the internal BPM pair or four usable outside BPMs needed for
     closure are unavailable due to freezing, the scan waits and retries for
     about 90 s before reporting the interruption.
  2. Scan width [mm] is the FULL width, default 0.8 mm. Scan -> fit -> set optimum
     first checks COD closure for a centered [-0.4,+0.4] mm window. If unavailable,
     it tries shifted windows of the same 0.8 mm width, nearest zero first with
     0.1 mm center increments. All windows contain the original zero, so the
     furthest permitted shifted endpoint is +/-0.8 mm. If no full-width window
     closes, try full widths 0.4 and 0.2 mm in order. At each width try the
     centered window first and then shift the center in steps of one eighth
     of the width. Custom widths also halve down to 0.2 mm. A requested width
     below 0.2 mm is tried as entered.
     A window aligned with the calculated current bounds is also checked when
     those bounds fall between regular center increments.
     Endpoints, midpoint and quarter points must pass COD/current/target checks;
     a previously failed interior point rejects a candidate window. Failed
     closure restores baseline currents before trying another window. Hardware,
     acquisition and BPM validity failures abort instead of triggering fallback.
     Range discovery performs no XSR acquisition. The selected bounds/full width,
     all range attempts and measured checks are displayed/saved. Insufficient
     corrector current margin or exhaustion of the scan windows skips the target
     after baseline verification. The queue proceeds to its next checked target.
     The accepted window is scanned at five equally spaced points using
     I(a)=I0+a*k (e.g. -.4,-.2,0,.2,.4 mm if the original window closes).
     Before XSR acquisition at each point, the scan checks outside X/Y and
     the reconstructed target state. Only if needed, bounded beam-based iterations close leakage while
     retaining target amplitude/angle. No clipping or cumulative amplitude steps.
     XSR polls once per second and collects 10 fresh accepted readings. Up to
     three invalid readings, spikes or timeouts per point are retried. Spikes
     exceed both 10% of the point median and six scaled median absolute deviations.
     Raw values, means +/- SEM and fit
     curves are plotted live. The fitted horizontal coordinate is the observed
     local displacement; requested amplitudes are saved separately. A weighted
     quadratic fits sigma_y squared and is
     displayed as sigma_y. Five coarse points establish the first fit. The
     fitted minimum and its nominal 95% interval are calculated by testing
     where the derivative of that quadratic is consistent with zero. A
     bracketed interval within 0.05 mm on each side of the optimum allows
     immediate final verification. Otherwise, evaluate the expected interval
     at each candidate (default best +/-0.1 mm), measure the more informative
     side and refit. Up to two extra XSR points are taken. At that limit the
     minimum must have resolved positive curvature and its bounded interval
     must lie within the observed positions.
     For a minimum near an edge, extend the full window up to 1.0 mm about
     the selected window center and measure the outer endpoint on that side.
     A centered initial scan can reach -0.5 or +0.5 mm. The BPM closure and
     current limits also apply here; known failed range points are respected.
     If further expansion is unavailable, check between the outer points.
     A flat scan or unresolved/unbracketed minimum restores and verifies
     baseline and skips the target. Successful scans use 6 to 8 XSR points,
     including the independently measured final optimum. The final size must
     be consistent with the best measured point before leaving the currents set.
     Each fit uses a SEM floor based on the five coarse points and up to 20
     recent same-machine summaries. The historical estimate uses median SEM
     and (from three prior scans) the spread of verification errors. A
     timestamped .xsr.json under flight-simulator/DRTuning/<year>/ saves point
     statistics and fits. Older summaries in op/tempfiles remain readable.
  The SD/SF-labelled XSR plot for each completed target is saved under
  /mnt/nas1/atf-data/op/tempfiles/. On error, Resume retries the interrupted
  stage. A scan restored to its baseline reuses completed XSR points. A failed
  fit repeats the affected fit measurements. Save and end writes completed and
  partial measurements under /mnt/nas1/atf-data/flight-simulator/DRTuning/<year>/.
  Cancel, stalled acquisition, invalid fit or failed closure restores the
  pre-scan selected currents. Restoration attempts all supplies and reports
  failures; a failed restoration is never reported as success. Baseline reset
  and JSON/PNG export are available after completion. Native transverse RFTrack
  has no radiation-equilibrium XSR signal: it cannot run an actual size scan.
  No physical beam-size signal is synthesized for that backend.

Offline verification covers timestamp gating, duplicate updates, alarms,
timeouts, readback-normalized probes, nonlinear leakage refinement, physical
amplitude preservation, fit/refinement/final-setting, stale inputs, limits,
cancel/failure restoration and actual before/after dispersion. These tests do
not establish live-machine optics accuracy or correction performance.

SAD COD reference preparation and readback diagnostics (2026-10-23)
----------------------------------------------------------------
On the Machine interface, correction pages now default to RFTrack model
response. COD maximum corrector counts default to H=5/V=5, dispersion H=10/V=5,
following the archived configuration (editable, not live PV values).
RFTrack model > Model preparation defaults to "SAD COD reference" on Machine;
simulation retains "All captured currents" unless explicitly changed.
The SAD preset reproduces the archived CODCorrection preparation:
- Import main quadrupole strengths; include QM trims except QM7, omit QF trims.
  Read and record all trims, including ignored ones.
- Multiply the included main+trim strength by correctQK1[1] factors in the
  bundled sad_cod_calibration.json (98 entries, source hash and name aliases).
- Apply setBH1RK1[] from the COMMON Ring initialization in atfdrex.sad,
  before the CODCorrection branch: BH1R.1..6 K1L=-1.17442, BH1R.7..36
  K1L=-1.14780 [1/m] (the SAD bends have length 1 m). Earlier versions missed
  this initialization because the duplicate call inside COD was commented out.
  An explicit bend_k1l configuration takes precedence. These are model settings;
  no machine bend currents are changed.
- Set corrector/skew excitation to zero ONLY in the reference model. Virtual
  excitation is relative to the captured current; proposal centers and current
  bounds still use absolute machine currents. SET is I_captured + delta.
- Match QF1R/QF2R to configured tune targets, within +/-5% by default. Unstable trial steps
  are rejected and shortened. If the SAD COD initial reference is unstable and
  model tune matching is enabled, first search a nearby stable QF1R/QF2R point
  on an expanding bounded grid up to the configured limit. Every trial and the
  final fit stay within that limit of the imported, calibrated family strengths. Final optics must
  pass the unchanged eigenvalue modulus tolerance 0.0002 and tune residual
  tolerance 1e-5. Failed search/fit restores model strengths and produces no
  correction. All captured currents / matching disabled still fail immediately.
  This is an RFTrack initialization aid, not an exact SAD matching algorithm;
  it does not establish that imported currents or the lattice are correct.
  The proposal banner/model status indicates initial recovery; tune_match JSON
  stores initial_stable_point_search and family_fractional_changes. No machine
  QF currents are written by the fit. Save model diagnostics retains the initial
  failure, captured settings and completed matching report.
Model factors are historical calibration, not a claim of today's accuracy.
This COD reference is also used for dispersion/skew when selected; it does not
reproduce the old branches' dependence on previously loaded SAD session state.
It is not a numerical-equivalence claim for the complete SAD optimizer.
Select All captured currents to return to the previous model preparation.
An explicit model calibration file overrides the bundled factor table.

For a model test: select RFTrack model, exclude unmapped BPMs / magnets when
using the bundled lattice, then Build model / optics. No machine writes occur.
Check the recorded inputs/tune fit before CALC. COD model CALC reads BPMs but
does not scan real magnets; dispersion/skew still perform their documented
diagnostic excitations. Save model diagnostics is available even after failure.

"Magnet readback did not settle" is an electrical readback convergence failure,
not an RFTrack optics failure. Errors now identify the magnet, set/read PVs,
target, last readback, signed difference, timeout and tolerance. Restoration is
attempted for every touched supply and checked; restoration failures retain
the original error. The deadline remains 10 s. At the operator's request the
bundled current tolerance is now 0.02 A (previously 0.001 A). This shared setting
also governs skew/quad readback and current snapshot comparisons. Custom site
profiles retain their explicitly configured value. Restart to reload profiles.
Do not infer that extending the wait or loosening tolerance fixes a stuck PV.
The custom measured-response scan remains available; this change does not yet
connect the SysID worker or import its DATA*.pkl archives.

Skipping faulty readbacks during Measured magnet response CALC:
Initial unreadable/nonfinite magnet currents are recorded and omitted, including
unselected supplies and quadrupoles in the state snapshot. A candidate supply
whose readback fails, times out, drifts during settling, or has no positive
adaptive probe span is excluded from the response matrix; scanning continues
with the next magnet. The scan attempts the original current before continuing.
If readback disappears after a probe, that restoration command is still sent.
Inspect "Skipped magnets / errors" and the detailed log for magnet names,
errors and restoration results. Save data + figures retains scan_errors,
ignored_readbacks and set_blocked_reason in the session JSON. When all candidate
magnets fail, no proposal is produced and the detailed error lists the failures.
An unverified probe restoration permits remaining measurements but blocks SET;
resolve the fault and run CALC again. Successful SET/RESET only writes the healthy
supplies involved in that correction (and required diagnostic excitations).
Known initial unreadable currents are omitted from SET's state comparison;
other captured currents still undergo the usual stale-setting/readback checks.
Missing BPM signal, RF failures, write failures and failed required horizontal
skew probes are not silently skipped. Model-based optics snapshots remain strict.
The convergence timeout/tolerance are unchanged; skipping occurs after failure
is detected, not immediately when the target and readback first differ.

No valid enabled BPMs / actuators:
CALC now reports enabled BPMs, acquisition-valid BPMs, usable BPMs and enabled
actuators separately. It includes per-setting valid counts, nonfinite position
and invalid-intensity counts, and packed waveform status histograms (required
DR:monitors status=1). All samples/settings must share at least one valid,
enabled BPM. Selection exclusions are distinguished from acquisition failure.
Measured scans stop before magnet probes if the initial signal has no usable
BPMs; loss of all common BPMs during a probe aborts and restores that probe.
This does not weaken status/intensity validation or change waveform mapping.

Correction plot reading:
Black points = measured before SET; green dashed = predicted after SET;
red = measured after SET (only when applied); gray dotted = target in the fit
plane. CALC completion means a proposal exists, not that SET was performed.
Invalid/excluded BPMs are hidden from every curve and lines break at those
locations. Their count is displayed; raw data remain in saved sessions. Large
values at VALID enabled BPMs are not clipped. The fitted correction component
is retained in the saved data but no longer overlaid as another orbit curve.
Live COD values use DR:monitors raw / 1000 = mm, as in InterfaceATF2_DR/SysID.
In DRTuning's measured COD and Skew modes, adaptive probing defaults on
for Machine and RFTrack. Each endpoint uses 10 BPM updates by default.
COD measures the RMS orbit difference in the selected plane. Skew measures
the change of the two vertical difference orbits excited by the horizontal
steerers. The noise estimate propagates the standard errors of those readings.
Minimum response S/N defaults to 5. A sufficient column is accepted immediately.
A weak column alone is remeasured with a larger probe, within the supply
and probe limits. A column still below threshold at the limit is omitted.
Model responses use fixed finite differences. The Bump shared and per-target
matrices apply the same acceptance rule to the measured X/Y orbit response.

XSR emittance estimate (2026-10-23)
---------------------------------
Open In development > XSR emittance in the left navigation or Measurements menu. This worksheet
estimates vertical emittance only; horizontal inputs and PVs are not required.
1. Measure absolute beta with Q modulation or calibrated TBT / phase beta,
   then run SR / SR2 beta fit. Use Estimate XSR emittance in that fit dialog,
   or Use latest SR beta fit / Load SR beta fit in the emittance page.
   A BPM beta must first be transported to the emission point; relative TBT
   beta alone cannot give an absolute emittance.
2. Select the source point matching the actual XSR optical source (SR or SR2).
   Changing the source clears imported local beta/dispersion. Verify that the
   optics and size measurement refer to the same source and machine settings.
3. Read XSR vertical size [um] on the Machine interface, or enter vertical RMS size.
   Read-only PV: XSR:PROJ_V_SIGMA. The operator confirmed
   source-plane micrometres and completed resolution correction on 2026-10-23.
   The legacy reader already applies PROJ_*_SIGMA_FACTOR; never multiply it
   again. Additional resolution defaults to zero and resets to zero on PV read.
   This reads existing images; it does not trigger an acquisition or establish
   exposure freshness. The saved timestamp is the read time.
   RFTrack has no XSR size monitor attached here; manual/file optics input is
   supported on every interface, without inventing simulation measurements.
4. Optional: import the SR dispersion fit, enter RMS dp/p as a fraction, then
   enable Remove known dispersion contribution for the selected plane.
   Nonzero eta requires positive dp/p. Eta=0 explicitly represents negligible
   local dispersion. Without this correction, output is labelled apparent.
5. Estimate emittance reports geometric pm rad using
     epsilon = (sigma^2 - (eta*sigma_delta)^2 - extra_resolution^2) / beta.
   Beta Y and vertical dispersion are used. A nonpositive residual variance is
   unresolved, not clipped to a zero emittance. Optional independent 1-sigma
   errors use linear propagation; unknown errors remain unknown. Strong
   coupling or betatron/energy correlations invalidate the uncoupled formula;
   this is an effective estimate, not normal-mode emittance separation.
6. Save result and plot writes xsr_emittance.json plus one PNG. Inputs, units,
   corrections, source-fit / PV provenance and uncertainties are included.
   Edits invalidate the displayed result. No magnets are written by this tool.

Offline verification includes a synthetic measured-beta transport followed by
XSR size inference (12 pm rad recovered), unit conversions, dispersion and
optional resolution subtraction, uncertainty propagation, invalid signals,
read-only PV acquisition, GUI navigation, saving and input invalidation.
This is not a new RFTrack equilibrium-emittance or live-machine validation.
Physical context: KEK Report 2020-4, emittance measurement section,
https://cds.cern.ch/record/2742899/files/CERN-ACC-2020-0029.pdf

SET count and outcome verification (2026-10-23)
----------------------------------------------
Maximum H / V correctors for SET are visible on the correction pages. The
Machine GUI defaults now follow the archived values below; other backends
retain 8 / 8. The archived sad/control/db_sad.adb records
COD H=5,V=5 and dispersion H=10,V=5; these are historical configured maxima,
not evidence of today's live PV values or actual selected counts. The old
solver adds useful correctors until the maximum or residual tolerance is met.

After CALC, the result banner and SET button show the actual number of magnet
settings to change. The Magnet settings tab distinguishes Will change from
No write. SET now writes only changed settings, using the same definition as
the display; diagnostic RF / horizontal probe scans still run when required.
A zero-change proposal disables SET. No additional confirmation popup is added.

After SET, the existing remeasurement and measured/predicted overlay remain.
The SET verification tab opens automatically and reports, for the selected
correction plane (or the two skew probe signals):
- common valid BPM count, before / predicted / after RMS relative to target;
- improvement = 100 * (1 - after_RMS / before_RMS), positive being better;
- RMS and maximum absolute measured-minus-predicted difference, and worst BPM.
Dispersion and COD are evaluated separately, in mm/(dp/p) and mm. Invalid or
nonfinite readings are excluded from all three RMS values on the same mask.
No valid BPMs means unavailable, never successful. Ratios with a zero reference
are unavailable. Improvement is descriptive, not a noise-significance test;
prediction mismatch can include noise, drift, calibration or model errors.
No automatic repeat correction or rollback is triggered by this assessment.
Use RESET to undo a completed correction if required. Existing acquisition
failure rollback remains in place. Save data and plots includes assessment,
per-BPM mismatches and applied_actuators in the correction JSON. Re-CALC clears
old outcome data. The other plane remains visible in the measured/predicted plot.

Settings-driven RFTrack model response (2026-10-23)
-------------------------------------------------
The correction pages now offer "Measured magnet response" and "RFTrack model
response". The existing measured method remains the default. RFTrack model
response is an added calculation route, NOT a claim that the old SAD programs
have been reproduced numerically in every detail.

Operator workflow:
1. Open In development > RFTrack model in the left navigation. Machine input defaults are the
   old SAD energy/tune target PVs; no user-selected JSON is required. Tune PVs
   are settings, not asserted fresh TBT measurements. RFTrack simulation uses
   separately excited measured BPM waveforms to supply its tune targets.
2. If using the bundled historical lattice with the live channel list, use
   "Exclude unmapped BPMs / magnets". It explicitly excludes MB97R/MB98R and
   fixes ZH15R/ZV16R, absent from that lattice. Their settings are recorded as
   unmodelled fixed actuators. They are never silently mapped to other devices.
   A current lattice export can instead supply the missing geometry.
3. Build model / optics captures public settings and matches QF1R/QF2R in the
   MODEL only. No real magnet is written. The tune fit, beta plots and source
   settings can be saved with Save model / optics.
4. Choose RFTrack model response on COD / Dispersion / Skew. CALC recaptures
   current settings, validates mappings/calibration/stability, matches tunes
   and computes the response before requesting diagnostic excitation.
   COD reads the orbit only. Dispersion uses one real symmetric RF pair plus
   a nominal orbit. Skew uses the two real horizontal probe scans. Corrector
   or skew RESPONSE columns are computed inside RFTrack, without scanning
   those supplies on the machine. CALC restores diagnostic excitation.
5. Inspect measured / predicted curves and proposed currents, then SET.
   SET retains current-limit, stale-setting, remeasurement and rollback guards.
   Main-supply, tune-target and calibration PV changes also invalidate a model
   proposal. RESET restores the pre-SET snapshot as before.

Model-only choices on correction pages:
- Match model tunes: QF1R/QF2R family scaling, default bound +/-5%, residual target
  1e-5. Stable symplectic eigenmodes retain the >0.5 tune branch. Real-waveform
  aliases are resolved only when the input explicitly identifies alias tunes.
  Failure to converge aborts before diagnostic writes. Model fit changes never
  become QF current commands.
- SAD straight-section targets (H): use nominal horizontal dispersion outside
  MB19R..MB31R / MB66R..MB80R and zero within; retain model orbit targets in
  those regions. This is explicit, not applied to measured-response runs.
- Preserve predicted extraction orbit / dispersion: add weighted zero-change
  rows at the KIX occurrences and MB19R/MB20R/MB21R. They constrain position AND
  slope in the selected plane. These are SOFT model constraints, not a guarantee
  of unchanged extraction on the machine. The weight is in advanced settings;
  predicted constraint residuals, units and locations are saved. This extends
  the legacy position constraints; it is not numerically identical to SAD.

Model inputs and calibration:
- Read SAD:FILE:DR:ENERGY [GeV], SAD:DR_TUNE_X/Y, main quadrupole settings and
  trim readbacks, SD1R/SF1R main settings, corrector/skew readbacks and limits.
- Main + trim use mag_ki_main separately. The public hardware QM7R.1 mapping
  is applied to lattice QM7AR.1. Normal SD1R K2L uses SetRingSext's negative
  sign convention. Skew K1L/A is read from SAD:SKEWCOR_SDCALIB/SFCALIB.
- All required quad and sextupole strengths must be supplied. No missing live
  calibration is replaced with a guessed field. Invalid active mappings fail.
- Bend geometry/gradients and RF-disabled transverse tracking come from the
  selected versioned lattice. This does not infer magnet alignment, radiation
  equilibrium, or a fully identified real-machine lattice from two tunes.
- Optional model calibration JSON: schema="dr-model-calibration-1", source,
  quadrupole_factors={physical_name: multiplicative_factor}, and/or
  bend_k1l={physical_name: integrated_gradient}. Factors act on the included
  main/trim K1L according to the selected preparation preset.
  Legacy DR_QUAD_CORRECTION.DAT has different conventions; it is NOT silently
  multiplied into these factors. Supply an audited conversion for site use.
- Optional generated lattice JSON can be selected without changing the machine
  interface. Model inputs, raw PV values, ignored fixed actuators, lattice hash,
  tune matching and assumptions are saved in every model correction dataset.

Model tune matching at a QF bound:
The Newton update now solves a bounded linear least-squares problem using the
remaining family ranges and a 0.5% per-iteration step cap. It no longer merely
clips an unconstrained Newton direction, which could stall at one QF bound
while improvement in the other family remained possible. Unstable trials are
still rejected and backtracked; final stability and tune tolerances are unchanged.
RFTrack model > Model QF matching limit (±) controls the shared model-only bound
for Build model and COD/dispersion/skew CALC. Default 5%; editable 0.1..20%.
Changing it invalidates existing proposals; it does not write real QF currents.
Start with the default after updating. If the failed fit still reaches a bound,
10% can be tried explicitly with Build model / optics. Inspect fitted QF changes
and save diagnostics; reaching the tune targets alone does not validate beta or
prove that the imported lattice/calibration matches the machine.
The selected limit is captured as tune_match_max_scale in settings and max_scale
in the matching report. Offline model_validation --settings uses the recorded
limit (5% for old files). Limits are never expanded automatically on failure.

Debugging unstable one-turn maps:
- The error describes the imported RFTrack model, not measured beam stability.
  Initial imported optics are checked even when model tune matching is disabled.
  The stability threshold remains unchanged (max abs(abs(lambda)-1) <= 2e-4).
- Preparation errors now identify initial optics vs QF1R/QF2R tune-match trials.
  Unstable-map errors include eigenvalue moduli; failed trials also record QF
  fractional changes and restore the local model before propagating the error.
- After a failed model CALC or Build model / optics, open RFTrack model and
  Save model diagnostics. The JSON records the last preparation attempt,
  stage/status, raw PVs, calibrated strengths, design-reference quad strengths,
  current snapshot, tune targets, and matrix/eigenvalue diagnostics when present.
  If tune matching fails, tune_match_attempt also retains the target/resolved
  tune branches, stable seed, evaluated stable points, rejected unstable trials,
  accepted iterates, final residual and active configured bounds. Model strengths are
  restored after failure; the retained final values describe the failed trial.
  A bound hit does not prove that no solution exists elsewhere inside the bounds.
  Saving does not read or write the machine. A partial capture failure may not
  have complete settings. Failed models cannot be used for a correction.
- Supply the diagnostic file to distinguish calibration/geometry/input issues
  from an unstable matching trial. Do not change real QF currents to make the
  offline model stable. Stable nominal fake-EPICS input coverage is not proof
  that current live-machine settings have been reproduced.

SR / phase optics:
- Build model first, then "Use RFTrack model" in SR beta or dispersion fit.
  The machine must still be at the captured settings. Independently supplied
  optics files remain supported. Phase beta also has a model transport button.
- SR/SR2 are evaluated at fractions .27/.9 INSIDE BH1R.27, including the entrance
  edge and no premature exit edge. These are fractions, not metre offsets.
- SR beta uses RFTrack transfer matrices and the existing constrained Twiss
  fit. Q-modulation panels can pool at least three measured quad betas for this
  fit; five-point Gaussian quadrature averages transport through each magnet,
  rather than silently treating a magnet-average beta as its centre beta.
- SR dispersion uses upstream steerer response modes evaluated at MB64R..66R
  and the two SR positions. The same two independent modes per plane are fit at
  BPMs and transported to SR; rank-deficient fits are rejected.
- The uncoupled beta/phase formulas explicitly reject appreciably coupled
  model maps. Their outputs can now feed the XSR emittance estimate above.
  They do not optimize emittance directly.

Reproducibility and independent comparison:
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=atf2-knobs \
    python -m Knobs.DRTuning.model_validation --native --output /tmp/dr-model-check

This offline check uses a separate RFTrack plant with an unreported 5 um quad
alignment error and an additional unreported roll error for skew correction.
It tests measured improvement, restoration, tune matching, SR transport fits
and extraction constraints. It uses no real EPICS clients. Saved model inputs
can also be replayed with --settings <model.json> --output <directory>.

Validation on 2026-10-23: 117 DRCorrection / DRTuning unit and integration
tests passed. The native check completed in 46.1 s; measured residual vector
norms changed as follows (different units per task; these are NOT emittance):
  COD:        0.114517 -> 0.00370019   (96.8% reduction)
  Dispersion: 71.6592  -> 2.07484      (97.1% reduction)
  Skew:       0.0155995 -> 0.0136015   (12.8% reduction)
Commanded settings were restored. This is one noise-free fixture, not a
multi-seed performance claim or a benchmark against SAD.

"Compare reference file" supports dr-optics-reference-1 (source, tunes,
rows={name:{s_m,beta_m:[x,y]}}, optional settings_sha256) and
dr-response-reference-1 (source, mode, names, actuators, actuator_unit,
signal_units, response, optional delta). Signal units are "mm" for orbit/skew
or "dispersion:mm/delta;orbit:mm" for dispersion. Matrices are name-aligned;
units and geometry are checked. Independent reference values are not invented
from the model under test. --reference accepts an optics reference for replay.

The locally archived SAD executable requires a missing 32-bit Linux loader;
native SAD-vs-RFTrack numerical equality and live commissioning are therefore
NOT established by the offline tests. Match the deployed lattice/calibration
and compare independent SAD exports before relying on model accuracy.

The earlier sections below describe the measured-response route unless noted.

Start from flight-simulator-framework:

  PYTHONPATH=atf2-knobs python -m Knobs.DRTuning

Or launch Knobs/GUIs/DRTuning_GUI.py directly with the same Python interpreter.
After package installation, the DRTuning GUI entry point is also available.
The launcher opens the configured Machine (EPICS) session directly. To choose
RFTrack, Stored DR or DEMO, launch with --select-interface.
At startup the yearly DRTuning data folders are searched for compatible completed
QM trim, QM main, vertical ZV and shared bump matrices. Switching QM coil selects
its saved matrix. Recalculate uses a current beam observation and current magnet
settings. Apply is a separate action. With no compatible file, measure normally.
Measurements, Corrections and Utilities menus expose all the former launcher
tools, including BPM/magnet selection, orbit buffers, Delta-f Ramp and SR fits.

Mouse wheel / trackpad scrolling over numeric inputs and closed dropdowns
scrolls the surrounding panel without changing the setting, even if the input
has focus. Edit numbers with the keyboard or the spinbox arrow buttons; open
dropdowns explicitly to choose an item. This applies to the launcher, all DR
tuning panels, and their settings dialogs, including dialogs opened later.

RFTrack: native RF-Track transverse DR, reference 20111111b lattice, no radiation
or 200-ms equilibrium assertion. BPM positions come from tracked probes, with
1 um RMS simulated read noise. Quadrupole main coils hold design strengths;
individual trims use the existing mag_ki_main calibration and simulated +/-2 A
limits. Correctors retain the existing calibration and +/-5 A current bounds.
This is a diagnostics port validation environment, not a new emittance result.

Machine (EPICS): select Machine (EPICS), then OK. The bundled machine settings
are used automatically (internally stored in hardware_profile.default.json).
No user-selected JSON or user-specific path is needed. Opening the interface
loads configuration and builds the GUI without reading or writing machine PVs;
individual measurements check their PV connections. The programmatic
load_hardware_profile(path) API remains available for site tooling/tests.
Stored DR (200 ms): retains the previous fast stored-beam corrections/TBT adapter;
this optional backend does not yet expose quadrupole trims. It is NOT RFTrack.
DEMO: synthetic correction/TBT data for UI checks, not quadrupole commissioning.

One interface is shared by COD, dispersion, skew, TBT, four Q-modulation panels,
BPM buffers, fixed-steerer selection, Delta-f Ramp and SR/SR2 dispersion fit.
They open in one session window so scans cannot race each other. Closing that
window closes its interface. An active scan must finish restoration before
closing. Cancellation is available during Q modulation
and TBT acquisition. Existing response CALC/SET/RESET behavior is retained.

Control-room UI (2026-10-21 redesign)
-----------------------------------
Launcher, TBT, Q modulation and correction workspaces use resizable layouts,
11-point controls and left-hand tool navigation. English operation labels avoid
missing-glyph boxes on PCs without Japanese fonts. Settings scroll vertically;
the active plot stays beside them. The window supports 1024 x 768 logical pixels.
The status strip always shows working/completed/failed state and elapsed time.
Failures open the detailed log automatically; its text can be selected/copied.
Cancel remains available while controls are locked during an operation.

First live TBT check: Machine (EPICS) -> OK -> Measurements / TBT waveform / beta ->
Read waveform (Update). Acquire a new shot is OFF by default: this reads the
existing WPSH/WPSV/WINT records without SNAP, PROC or mode writes. The progress
strip shows the current PV and progress through 288 waveform reads. Missing PVs
and short arrays identify the affected PV. Cancellation is checked between
reads; it must wait for an in-flight EPICS read timeout. Failed reads preserve
the previous displayed waveform. Real control-room acquisition is still not
validated by offline GUI tests.

Q modulation
------------
QF1R (28), QF2R (26), QM trim 1 (23), QM trim 2 (23) retain five points per
magnet, USE/OFF and read/fit/save operations. A magnet summary replaces the huge
legacy grid; select one magnet to inspect/edit its five-point table. The Tune
fits tab shows its plots at full size. Original source forms and PV inventory
remain archived in forms/inventory.json; their fixed geometry is no longer used
for the Q-modulation screen. No old BETA:* result PV is written.

Read captures current and measured H/V tunes. Double-clicking a table value edits recorded
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
The TBT screen replaces the fixed Turn1024 geometry with grouped acquisition,
display and calibration settings. The large plot selector switches between
waveform, FFT, beta, phase and a four-panel overview for larger windows. The
Matplotlib toolbar supplies pan/zoom and plot export. Statistics, automatic
saving, beta calculation and file operations remain available. Advance/rewind
first turn operates on each NEW acquisition only. This port does not subscribe a perpetual background
loop to every beam pulse. The extraction checkbox selects a configured
extraction_turn; it does not start the old external extraction-shot daemon.
TESTMODE is represented by the explicit DEMO interface at the launcher.

RFTrack starts at turn 1; choose Fit start turn to omit the excitation passage.
It does not track 471150 native turns to emulate the legacy extraction setting.
The optional stored backend is the separately qualified 200-ms model.

Amplitude beta: Calculate tune / relative beta uses the existing sine-fit analyzer. Relative
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
New-shot acquisition first reads DRBPM:ORBIT_MODE, temporarily sets it to TBT
(0), waits for matching readback (5 s deadline), then settles for 0.5 s and
checks the readback again. Only then does it execute the crate sequence below.
After restoring crate mode / shot mode / first turn, it restores the ORIGINAL
ORBIT_MODE and verifies it, also on acquisition failure or cancellation.
The original mode may be TBT=0, COD=1, Injection=2 or Extraction=3; it does not
unconditionally select COD on exit. No ORBIT_MODE write is made if already in
TBT. A restoration failure is reported explicitly, never as successful capture.
Successful results record orbit_mode_control with the original/acquisition mode
and restoration status. This guard applies to both acquisition strategies.
The profile fields orbit_mode_pv, orbit_mode_timeout_s and orbit_mode_settle_s
configure the PV and timing; omitted fields use the defaults above.
Read waveform with Acquire a new shot OFF remains read-only and does not change
ORBIT_MODE. COD / Injection controls in other tools are not changed by this.

The user confirmed that there is no dedicated all-BPM completion PV. Default
readback_checks therefore ports the Turn1024 one-shot / shot-mode / TURN / SNAP
sequence and uses TBT:TUNE:H timestamp updates as the old event source. It then
processes the BPM records, requires every waveform timestamp to advance, checks
that waveform content changed since the pre-SNAP baseline, and requires two
reads with identical waveform content while the event
timestamp remains unchanged. Stale, partial and torn reads time out. All original
mode/turn values are restored, also on failure or cancellation.
Waveform-check timeouts now report counts and names (first eight per category)
of stale/missing or payload-unstable PVs, timestamp-only updates, timestamp
regressions, unexpected PVs, whether waveform content changed, and whether the
tune event changed during the read. Repeated record processing may advance a
timestamp without changing a waveform: this alone is no longer classified as
payload instability. Timestamp regression is still rejected. New data must still
differ from the pre-SNAP payload; processing an old waveform is not accepted as
a new shot. Successful provisional results save both passes of timestamp/hash
evidence and the list of timestamp-only updates. This does not establish a
common shot ID. First-turn and shot-mode readbacks are also checked before SNAP
and around waveform reads to detect interference from another acquisition client.

For content_changed=False, first compare with a working acquisition in the
existing Turn1024 tool. Then read those buffers in DRTuning with New shot OFF.
For a subsequent DRTuning new-shot test, use the SAME first turn as the working
tool and avoid simultaneous trigger/turn control by another client. The archived
Turn1024 initializes m_turn=369350 (2024-06-28); DRTuning starts at 1. The old
constant is not automatically a valid current operating setting. A tune update
alone cannot demonstrate that the requested waveform has been acquired.

These are SOFTWARE readback checks, not proof of a common hardware shot ID.
Every resulting measurement has same_shot_verified=false and records the IOC
timestamps/content hashes. The GUI and Q-modulation results state this limitation.
A future genuine completion counter can use mode=completion with generation_pv
and completion_confirmed=true. Never point it at a tune value and claim it is
an acquisition counter. Acquisition settling defaults (1 s arm, 1 s readout,
50 ms stable reread, 30 s timeout) are provisional settings, not measured machine
performance. Read-only previously acquired waveforms remain available by
unchecking Acquire new shot.

操作ガイド（DRTuning）
---------------------
QM dispersion は Select matrix で Trim / Main の実測行列、Vertical dispersion は
Select ZV matrix で実測ZV行列を選ぶ。選択欄に種類、行列の測定日時とファイル名が出る。
Update dispersion は行列の選択前でも使える。RFを切り替え、現在の分散とCODを測定・表示する。
Recalculate は磁石の現在値と許容範囲を読み、同じ設定で最後に測った分散から提案を作る。
分散が未測定、または磁石設定・測定条件が変わった場合は自動で再測定する。
提案電流と予測曲線を確認し、Apply を押す。中断されたQMの応答測定は Resume interrupted scan から再開する。

COD correction は水平または垂直の閉軌道を補正する。Plane と使用する
BPM・磁石を選び、Measure response (CALC) を押す。RFTrack model response
では選択面の軌道を測り、行列をモデルで計算する。Measured magnet
response では ZH または ZV を順に振り、実測行列を作る。Magnet settings
と予測軌道を確認し、Apply correction (SET) で反映後の軌道を測る。
Undo correction (RESET) は直前の SET 前の設定へ戻す。

Skew correction は水平励振で縦へ漏れる軌道応答を小さくする。
異なる水平コレクターを probe1・probe2 に選び、各 full span と SD/SF
の使用群を設定する。CALC は各水平コレクターの ± 電流で縦軌道を測る。
Measured magnet response は各 skew 電源の両端でもこの測定を繰り返す。
SET 後は同じ励振で残留応答を測定する。XSR の縦ビームサイズを目的に
最適化する場合は Local bump / XSR coupling を使う。

COD・Skew の実測行列は Save data and plots で保存できる。同じ面・励振条件で
Measured magnet response を選び、Calculate from file で保存結果を開く。
Recalculate (saved response) は保存時のビームで再計算する。
Reuse measured matrix on current beam は現在の電流・制限とビームを測り直す。
Skew では水平励振2台を動かす。補正の適用は SET で行う。

TBT / beta は既存波形なら Read waveform (Update)、保存波形なら Open file
を使う。BPM・Signal・Fit start turn を選び Calculate tune / relative beta
で解析する。無効BPMと補正画面で除外したBPMはフィット対象から外れる。
Waveform には元データを残す。絶対β[m]には既知の参照βを設定するか、
独立の位相光学を読み込んで Calculate phase beta を使う。
Acquire a new shot は対応バックエンドでTBTモードを一時設定して取得し、
取得終了時にモードを戻す。実機での励振は外部exciterを使う。
このタブから補正電流を適用する操作はない。

Calibration files and remaining site configuration
-------------------------------------------------
Measure response (CALC) explicitly measures/replaces the response. Recalculate
(saved response) reuses the current plane/source's saved response and beam data,
without BPM acquisitions, RF changes or magnet scans. H/V results are retained
independently when changing controls or switching planes. Use this for SVD,
maximum corrector count, current bounds, fit weights and BPM/magnet subsets.
Changing the adaptive probe target configures the NEXT measurement; recalculation
does not improve the old response's noise or retroactively change its excitation.
The GUI shows the target used at acquisition. Saved JSON keeps acquisition_options
and acquisition_valid separately from the current fit options/selection.

Newly included magnets need measured columns, and an unmeasured plane needs its
first scan. Changed signal definitions (e.g. momentum compaction or skew probe
span) and changed model inputs require a new response calculation. A cached fit
uses the old beam observation: SET still checks the original machine baseline
and rejects settings changed since CALC. Cache and H/V targets are in-memory;
Save data and plots / Calculate from file remain available for disk storage.

During COD/dispersion/skew CALC, Pause requests a stop at the next restored
probe-pair boundary (including adaptive amplitude retries). It does not freeze
the machine at a displaced RF or magnet setting. Wait for PAUSED in the status;
Resume continues with measured columns retained in memory. Cancel also works
while paused and ends CALC without a completed proposal. Pause is unavailable
for SET, file recalculation and other tools. RFTrack response calculations share
the same controls; model preparation must finish before reaching a checkpoint.
Settings are checked before pausing and after resuming; changed settings require
a fresh CALC, without overwriting the intervening changes. This is an in-process
pause, not a saved checkpoint for restarting the application.

The default live horizontal nominal dispersion target is unset (null); vertical
is zero. Measured horizontal response CALC requires Load target dispersion,
with JSON fields names, x, y (dispersion in mm per relative momentum deviation,
one finite value per BPM). It aligns by BPM name. A missing horizontal target
now produces a specific message before any magnet/RF changes. Invalid lengths
or nonfinite entries are reported separately. RFTrack response uses its prepared
model target; model preparation must succeed first. Do not substitute zero for
the ring's nominal horizontal dispersion.

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
On an OFF-to-ON transition, RF scans wait an additional 1 s before selecting a
preset (rf.enable_settle_s), following op-base mOTRScanner. Already-ON scans do
not resend CONTROL_ON_SW. Preset checks always apply; physical RF readback
checks apply when rf.verify_frequency_readback=true (temporarily false in the
bundled profiles at the operator's request, as described above);
timeouts include read-only PV diagnostics captured before restoration. This
sequence is tested offline; it does not establish the cause of a live MI1
timeout. OFF retains SysID's CONTROL_OFF_SW=0 convention; the recovered Qt/mOTR
code instead writes 1, so that discrepancy still needs site verification.
During preset confirmation, a temporary zero/multiple selection is retried
until the existing timeout; initial snapshots remain strict, and HOLD,
invalid values and disconnected PVs still fail. Show detailed log records
DeltaFRamp requests, command PV/value, preset/frequency confirmation and
restoration through the worker's Qt signal. For a failed -1 -> +1 kHz scan,
inspect the PL1 command and the failure BEFORE restoration: an OFF display
afterward can be the scan's restoration to its original OFF state. These
offline checks do not establish why a live controller failed to switch.
Corrector/skew operations check current convergence without an extra fixed wait
(magnet_settle_s defaults to 0). As in SysID, correction measurements use repeated
BPM acquisition: the live GUI defaults to BPM samples per setting = 10 and BPM
interval [s] = 1, both editable. Waits occur before each read, including the first;
the total per setting is about 10 s plus communication time. Means, population
standard deviations and raw samples are saved. A BPM must be valid in every
sample to be fitted. Samples are not certified independent/fresh shots: COD
does not yet verify waveform timestamps/shot IDs. RF/quad waits remain separate.
After Apply correction (SET), COD/dispersion/skew verification waits an additional
5 s after all correction-current writes/readback checks complete. The status
shows this wait; cancellation restores the pre-SET settings without measuring.
Existing BPM sampling/RF settling follows this wait. CALC probe timing is
unchanged. Saved successful results record after_set_settle_s = 5.
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
