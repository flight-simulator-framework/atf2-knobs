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
  Exclude QM trims field contains these actual selections, not placeholder text.
  Defaults: per-QM initial widths inferred from the 2026-11-19 13:58 scan,
  targeting S/N 6.5 (30% above acceptance threshold 5), in A:
                 QM1R QM3R QM4R QM20R QM21R QM22R QM23R
             .1  .16  .34  .13   .62   .50   .28   .16
             .2  .22  .21  .18   .47   .28   .20   .29
  No checkpoint load is required. The GUI always uses per-QM starting widths.
  These are widths around the current allowed reference, not absolute currents.
  The fallback width is 0.1 A, used for a QM without an individual width
  (QM2R if explicitly enabled). It is editable in Measurement.
  Maximum current change per Apply is 3 A and RF full span is 4 kHz
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
  With one pair per default 13 active trims: 27 dispersion observations, 81 COD settings,
  810 BPM frames. Enabling all 16 gives 33 observations / 99 settings / 990 frames.
  Each extra adaptive pair adds 6 COD settings / 60 BPM updates. This can take
  several minutes with live settling.
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
  Show detailed log now records "BPM validity:" for each observation, including
  QM/current, current versus cumulative valid counts, lost BPM names, and
  per-RF/per-update counts. The saved JSON validity_history retains these
  diagnostics plus source status evidence and links to raw orbit measurements.
  Once excluded during a scan, a BPM stays out of that scan's common response.
  Dx is targeted to zero at legacy straight BPMs MB19R–MB31R / MB66R–MB80R.
  Arc Dx, Dy and COD target their measured baselines. These are named physical
  BPM regions, not an assumption that the entire ring has zero dispersion.
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
  CALC automatically writes qm_scan_*.checkpoint.json under /atf/data/flight-simulator/DRTuning/<year>/
  (or the chosen save folder). Baseline and each completed QM pair are saved;
  adaptive attempts and available raw frames are also recorded. A pair is
  committed only after both observations and current restoration succeed.
  Writes use a temporary file, flush/fsync, and atomic replacement so an
  interrupted write preserves the previous checkpoint. Saving failure stops
  the scan after restoration; it does not continue accumulating unsaved pairs.
  Resume unfinished QM scan uses the current partial result, or first choose
  Load QM scan checkpoint after restarting the GUI. Completed response columns
  are reused; the unfinished QM starts from a matching saved estimate, its
  enabled per-QM preset, or the uniform/fallback width. Loading a scan never overwrites the
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
  Presets do not silently switch to another combination or invoke RFTrack.
  Eligibility uses current readbacks, parameter A/B and max change from
  baseline, with nominal response only to estimate the reachable interval.
  A shifted interval with the fallback width (default 0.4 mm) is sufficient;
  the bidirectional calibration probe must also fit. Measured closure still
  determines the actual scan interval. Recheck after machine settings change.
  Rank SD/SF by |trim current| optionally sorts positions by signed current's
  absolute value; failed readbacks are listed separately.
  1. Calibrate and close bump automatically checks the selected preset against
     current BPMs and limits, then measures X/Y corrector response columns with
     10-update BPM averages. Both signs are tested and baseline currents are
     restored. Default max baseline current change is 4 A, outside max .01 mm
     and outside RMS .005 mm. Calibration uses the small +/-0.1 mm probe.
  2. Scan width [mm] is the FULL width, default 0.8 mm. Scan -> fit -> set optimum
     first checks COD closure for a centered [-0.4,+0.4] mm window. If unavailable,
     it tries shifted windows of the same 0.8 mm width, nearest zero first with
     0.1 mm center increments. All windows contain the original zero, so the
     furthest permitted shifted endpoint is +/-0.8 mm. If no full-width window
     closes, try width 0.4 mm, centered first then shifted in 0.05 mm increments.
     For other requested widths these increments and fallback scale proportionally.
     A window aligned with the calculated current bounds is also checked when
     those bounds fall between regular center increments.
     Endpoints, midpoint and quarter points must pass COD/current/target checks;
     a previously failed interior point rejects a candidate window. Failed
     closure restores baseline currents before trying another window. Hardware,
     acquisition and BPM validity failures abort instead of triggering fallback.
     Range discovery performs no XSR acquisition. The selected bounds/full width,
     all range attempts and measured checks are displayed/saved. No feasible
     half-width window means stop and restore; no further automatic narrowing.
     The accepted window is scanned at five equally spaced points using
     I(a)=I0+a*k (e.g. -.4,-.2,0,.2,.4 mm if the original window closes).
     Actual outside X/Y and reconstructed target state are checked at every
     point. Only if needed, bounded beam-based iterations close leakage while
     retaining target amplitude/angle. No clipping or cumulative amplitude steps.
     XSR takes 10 new updates at each point; raw values, means +/- SEM and fit
     curves are plotted live. The fitted horizontal coordinate is the observed
     local displacement; requested amplitudes are saved separately. A weighted
     quadratic fits sigma_y squared and is
     displayed as sigma_y. The minimum must have resolved positive curvature
     and be inside the measured scan range. Additional measurements at the
     first fitted best +/- .1 mm (clamped to the selected window) are
     followed by a refit. The final fitted optimum is set, with another 10 XSR
     updates and COD verification. Final size must be consistent with the best
     measured point. Successful completion leaves the optimum currents set.
     Normal runs have 5 coarse + 2 refinement + 1 final points = 80 XSR updates.
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
Measured scans can adapt each magnet's probe amplitude using BPM feedback.
"Adapt probe to BPM response (SysID rule)" defaults ON in Machine and RFTrack
simulation DRTuning sessions. It is disabled for RFTrack model-response CALC;
that path keeps fixed model finite differences and its existing live diagnostics.
Defaults: initial corrector probe +/-0.05 A, target response 0.1 mm, target band
+/-25%, maximum 8 measurement pairs per magnet, maximum probe +/-0.5 A about
the captured current. Supply bounds, including the corrector +/-5 A absolute
limit, always take precedence. All these adaptive controls are on the page.
Turning adaptation OFF keeps the prior fixed-amplitude measurement behavior.

Response means max(abs(BPM_plus - BPM_minus))/2 over enabled, common-valid BPMs
in the selected plane. Dispersion uses the nominal COD row, not eta; skew uses
the change in vertical responses to its horizontal probes. The shared
machine_interfaces.Backend.Excitation module holds SysID's existing update:
target/observed clipped to [0.8,2], then 80% new + 20% old; zero response uses
1.5 before smoothing. SysID_GUI imports the same functions. Its worker and
archive format are not replaced or invoked by DRTuning.

Every pair restores the original current before retrying. The final pair
provides the response column, normalized by its actual positive readback
difference. A zero/reversed/nonfinite current span aborts after restoration.
Target reached, amplitude/supply limit, and maximum-pair termination are
recorded distinctly. Unreached targets do not masquerade as convergence: the
proposal summary and detailed log identify them; a usable last measured column
is still available for the correction solver. All attempts, measured response,
commanded/readback currents and the selected final pair are saved in
probe_history alongside the raw orbit measurements. Cancel/invalid BPM paths
restore the current and do not proceed to the next pair or SET.
Programmatic Options defaults adaptation OFF for old scripts/saved sessions;
set adaptive_probe=True explicitly in an API caller. The new shared module is
preferred, but older machine-interfaces packages without Backend.Excitation
are supported by an identical bundled SysID rule in DRTuning and SysID_GUI.
The correction session records probe_rule_source. Missing dependencies inside
an installed shared module are not swallowed as a compatibility fallback.

XSR emittance estimate (2026-10-23)
---------------------------------
Open XSR emittance in the left navigation or Measurements menu. This worksheet
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
1. Open RFTrack model in the left navigation. Machine input defaults are the
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
First select an interface with the radio buttons and press OK, as in SysID.
The selector closes and the DRTuning measurement/correction window opens
directly. Cancel exits without creating an interface. There is no Initialize
step or profile-file picker. Close DRTuning and relaunch to choose another
interface; closing during a scan waits for cancellation/restoration first.
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

Calibration files and remaining site configuration
-------------------------------------------------
COD and dispersion remember Target BPM response [mm] separately for H and V
within the open GUI session (both initially 0.1 mm). This is the adaptive probe
amplitude goal, not the nominal COD/dispersion correction target.

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
