"""Bounded randomized excitation, durable command logging and pause semantics.

This module has no Qt/EPICS imports. A transport is supplied explicitly.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import math
import os
import threading
import time
import uuid

import numpy as np

from .catalog import Actuator, CATALOG, ICT_PVS, DR_MONITORS, DR_MODE, DR_TURN
from .progressive import ProgressiveRange, QualityTrip


@dataclass
class Config:
    hours: float = 8.0
    max_points: int = 0
    phase_half_range: float = 90.0
    timing_mode: str = "coarse"
    samples: int = 6
    guard_seconds: float = 1.0
    arrival_timeout: float = 15.0
    measurement_timeout: float = 10.0
    phase_tolerance: float = 0.5
    current_tolerance: float = 0.05
    max_changed: int = 10
    joint_every: int = 4
    reference_every: int = 10
    seed: int = 20261026
    score_ict: str = "DR"
    end_action: str = "initial"
    save_ict_summary: bool = False
    initial_radius_steps: int = 1
    expansion_points: int = 40
    expansion_coverage: int = 2
    max_step_steps: int = 1
    min_transmission_fraction: float = .10
    ict_max_age: float = 2.0

    def validate(self):
        for name in ("hours", "phase_half_range", "arrival_timeout", "measurement_timeout",
                     "phase_tolerance", "current_tolerance"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(self.guard_seconds) or self.guard_seconds < 0:
            raise ValueError("Invalid arrival guard")
        if self.phase_half_range > 180 or self.arrival_timeout <= self.guard_seconds:
            raise ValueError("Invalid phase range / arrival timeout")
        for name in ("samples", "max_changed", "initial_radius_steps", "expansion_points",
                     "expansion_coverage", "max_step_steps"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if any(type(getattr(self, n)) is not int or getattr(self, n) < 0
               for n in ("max_points", "joint_every", "reference_every")):
            raise ValueError("Invalid count / interval")
        if self.score_ict not in ICT_PVS or self.timing_mode not in ("coarse", "fine"):
            raise ValueError("Unknown ICT / timing mode")
        if self.end_action not in ("initial", "best", "keep"):
            raise ValueError("Unknown completion action")
        if not math.isfinite(self.min_transmission_fraction) or not 0 < self.min_transmission_fraction <= 1:
            raise ValueError("Minimum DR/LN0 must be greater than 0% and at most 100% of the initial ratio")
        if not math.isfinite(self.ict_max_age) or self.ict_max_age <= 0:
            raise ValueError("ICT maximum notification age must be positive")


_CURRENT_COMMAND_PVS = frozenset(p for a in CATALOG if a.kind == "current" for p in a.writes)


def _configuration_scalar(sample):
    """Read a connected, finite scalar without interpreting its record alarm."""
    try:
        raw = np.asarray(sample.value)
        value = float(raw.flat[0]) if sample.connected and raw.size == 1 else math.nan
        return value if math.isfinite(value) else math.nan
    except (TypeError, ValueError, OverflowError):
        return math.nan


def scalar(transport, name):
    sample = transport.sample(name)
    # currentWrite stores a command, not a measured current. Its record may
    # remain INVALID while the command field is usable. Keep sensors strict.
    value = (_configuration_scalar(sample)
             if name in _CURRENT_COMMAND_PVS and sample.severity == 3 else sample.scalar())
    if not math.isfinite(value):
        raise ValueError(f"No valid PV value: {name}")
    return value


def command_value(a, transport):
    values = [scalar(transport, p) for p in a.writes]
    return values[0]


def ctrl_bounds(transport, pv):
    info = transport.limits(pv)
    try:
        lo, hi = float(info["lower_ctrl_limit"]), float(info["upper_ctrl_limit"])
        return (lo, hi) if math.isfinite(lo) and math.isfinite(hi) and lo < hi else None
    except (KeyError, ValueError, TypeError):
        return None


def current_bounds(a, transport):
    # op-base/magnet/Qt/Library/MagnetLibrary/magnet.cpp: A=min, B=max.
    # These two fields store configuration, not beam/current measurements.
    # An unprocessed calc record can report UDF/INVALID for finite A/B values.
    # Sample.scalar() remains strict for measurements.
    values = []
    for pv in a.limit_pvs:
        sample = transport.sample(pv)
        value = sample.scalar()
        if sample.connected and sample.alarm_status == 17 and sample.severity == 3:
            value = _configuration_scalar(sample)
        values.append(value)
    lo, hi = values
    metadata = ctrl_bounds(transport, a.writes[0])
    if math.isfinite(lo) and math.isfinite(hi):
        if lo >= hi:
            raise ValueError(f"{a.name}: parameter.A must be less than parameter.B")
        source = "parameter.A / parameter.B"
        if metadata:
            lo, hi = max(lo, metadata[0]), min(hi, metadata[1])
            source += " ∩ CTRL"
        if lo >= hi:
            raise ValueError(f"{a.name}: inconsistent supply and CTRL limits")
        return lo, hi, source
    if metadata:
        return *metadata, "EPICS CTRL (parameter.A/B unavailable)"
    raise ValueError(f"{a.name}: parameter.A/B and CTRL limits unavailable")


@dataclass(frozen=True)
class Prepared:
    actuator: Actuator
    origin: float
    lower: float
    upper: float
    step: float
    grid_origin: float
    limit_source: str
    tolerance: float
    phase_wrap_low: float | None = None

    def grid_indices(self):
        return (math.ceil((self.lower - self.grid_origin) / self.step - 1e-9),
                math.floor((self.upper - self.grid_origin) / self.step + 1e-9))

    def random_value(self, rng):
        lo, hi = self.grid_indices()
        return round(self.grid_origin + int(rng.integers(lo, hi + 1)) * self.step, 10)

    def encode(self, value):
        a = self.actuator
        if self.phase_wrap_low is not None:
            value = (value - self.phase_wrap_low) % 360 + self.phase_wrap_low
        return {a.writes[0]: float(value)}

    def error(self, measured, target):
        delta = measured - target
        if self.actuator.kind == "phase":
            delta = (delta + 180) % 360 - 180
        return abs(delta)

    def validate_target(self, target, transport, exact_restore=False):
        if not math.isfinite(target):
            raise ValueError("Non-finite target")
        if not exact_restore and not self.lower - 1e-9 <= target <= self.upper + 1e-9:
            raise ValueError(f"{self.actuator.name}: target outside exploration range")
        if not exact_restore:
            tick = (target - self.grid_origin) / self.step
            if abs(tick - round(tick)) > 1e-7:
                raise ValueError(f"{self.actuator.name}: target is off the {self.step:g} grid")
        if self.actuator.kind == "current":
            lo, hi, _ = current_bounds(self.actuator, transport)
            if not lo <= target <= hi:
                raise ValueError(f"{self.actuator.name}: target exceeds current supply limits")
        for pv, value in self.encode(target).items():
            limits = ctrl_bounds(transport, pv)
            if limits and not limits[0] - 1e-9 <= value <= limits[1] + 1e-9:
                raise ValueError(f"{pv}: target exceeds CTRL limits")
            if not transport.can_write(pv):
                raise ValueError(f"No write access: {pv}")


def prepare(a, transport, config, override=None):
    origin = command_value(a, transport)
    if a.read:
        scalar(transport, a.read)  # Never substitute command echo for a missing sensor.
    if not all(transport.can_write(p) for p in a.writes):
        raise ValueError("Actuator is disconnected or read-only")
    grid_origin, wrap = 0., None
    if a.kind == "current":
        lo, hi, source = current_bounds(a, transport)
        if override:
            requested_lo, requested_hi = map(float, override)
            if (not math.isfinite(requested_lo + requested_hi)
                    or requested_lo < lo - 1e-8 or requested_hi > hi + 1e-8):
                raise ValueError("Requested range exceeds verified current limits")
            lo, hi = max(lo, requested_lo), min(hi, requested_hi)
        step, tolerance = .1, config.current_tolerance
    elif a.kind == "phase":
        lo, hi, source = origin - config.phase_half_range, origin + config.phase_half_range, "current phase ± range"
        step, tolerance = 1., config.phase_tolerance
        limits = ctrl_bounds(transport, a.writes[0])
        if limits and limits[1] - limits[0] >= 359:
            wrap = limits[0]
            source += " (360° wrap)"
        elif limits:
            lo, hi = max(lo, limits[0]), min(hi, limits[1])
            source += " ∩ CTRL"
    else:
        step, half = (11.2, 112.) if config.timing_mode == "coarse" else (2.8, 11.2)
        grid_origin = origin
        lo, hi = origin - half, origin + half
        limits = ctrl_bounds(transport, a.writes[0])
        if limits:
            lo, hi = max(lo, limits[0]), min(hi, limits[1])
        tolerance, source = 1e-6, f"LinacOpt {config.timing_mode} timing range [ns]"
    item = Prepared(a, origin, lo, hi, step, grid_origin, source, tolerance, wrap)
    first, last = item.grid_indices()
    if lo >= hi or first >= last:
        raise ValueError("Range must contain at least two grid settings")
    # Restoring the original off-grid setting is intentionally exact.
    item.validate_target(origin, transport, exact_restore=True)
    return item


def inspect(transport, config):
    ready, errors = {}, {}
    for a in CATALOG:
        try:
            ready[a.name] = prepare(a, transport, config)
        except (ValueError, RuntimeError) as exc:
            errors[a.name] = str(exc)
    return ready, errors


class Sampler:
    def __init__(self, items, config, progressive=None):
        self.items = items
        self.config = config
        self.progressive = progressive
        self.rng = np.random.default_rng(config.seed)
        self.groups = list(dict.fromkeys(p.actuator.group for p in items.values()))
        self.groups = list(self.rng.permutation(self.groups))
        self.queues = {}
        self.group_index = 0

    def _balanced_names(self, pool, key):
        count = min(len(pool), self.config.max_changed)
        queue = self.queues.setdefault(key, [])
        chosen = []
        while len(chosen) < count:
            if not queue:
                queue.extend(self.rng.permutation(pool).tolist())
            name = queue.pop()
            if name not in chosen:
                chosen.append(name)
        return chosen

    def next(self, index):
        if self.config.reference_every and index % self.config.reference_every == 0:
            return "reference", {n: p.origin for n, p in self.items.items()}
        if self.config.joint_every and index % self.config.joint_every == 0:
            kind, pool = "joint", list(self.items)
        else:
            kind = self.groups[self.group_index % len(self.groups)]
            self.group_index += 1
            pool = [n for n, p in self.items.items() if p.actuator.group == kind]
        names = self._balanced_names(pool, kind)
        if kind == "joint" and len(names) >= 2 and len(self.groups) >= 2:
            represented = {self.items[n].actuator.group for n in names}
            if len(represented) == 1:
                other = [n for n in pool if self.items[n].actuator.group not in represented]
                names[-1] = str(self.rng.choice(other))
        active = self.progressive.active() if self.progressive else self.items
        return kind, {n: active[n].random_value(self.rng) for n in names}


def clean_json(value):
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class Journal:
    def __init__(self, root, manifest):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = f"{stamp}_{uuid.uuid4().hex[:8]}"
        self.directory = Path(root).expanduser() / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.events = (self.directory / "commands.jsonl").open("x", encoding="utf-8", buffering=1)
        self.points_file = None
        try:
            self.points_file = (self.directory / "steps.csv").open("x", encoding="utf-8", newline="")
            self.points = csv.DictWriter(self.points_file, fieldnames=[
                "step_id", "kind", "measurement_start_utc_ns", "measurement_end_utc_ns", "quality",
                "sample_counts", "score", "requested_names", "ict_means_optional"])
            self.points.writeheader()
            self.write("session_start", manifest=manifest)
        except BaseException:
            self.close()
            raise

    def write(self, event, **payload):
        item = dict(event=event, run_id=self.run_id, utc_ns=time.time_ns(),
                    utc=datetime.now(timezone.utc).isoformat(), **payload)
        self.events.write(json.dumps(clean_json(item), ensure_ascii=False, allow_nan=False) + "\n")
        self.events.flush()
        os.fsync(self.events.fileno())

    def point(self, row):
        self.points.writerow(row)
        self.points_file.flush()
        os.fsync(self.points_file.fileno())

    def close(self):
        try:
            if self.points_file is not None:
                self.points_file.close()
        finally:
            self.events.close()


class Runner:
    def __init__(self, transport, selected, config, root, overrides=None, emit=None):
        self.transport, self.selected, self.config = transport, tuple(selected), config
        self.root, self.overrides = root, overrides or {}
        self.emit = emit or (lambda payload: None)
        self.cancel = threading.Event()
        self._condition = threading.Condition()
        self.pause_requested = False
        self.is_paused = False
        self.pause_action = "resume"
        self.running = False
        self.best = None
        self.journal = None
        self.items = {}
        self.state = {}
        self.visits = 0
        self.random_index = 0
        self.touched = set()
        self.progressive = None
        self.quality_held = False
        self._point_changed = set()

    def event(self, state, **payload):
        self.emit(dict(state=state, visits=self.visits, best=self.best, **payload))

    def request_pause(self):
        with self._condition:
            self.pause_requested = True
        self.event("pause_requested")

    def resume(self):
        with self._condition:
            self.pause_action = "resume"
            self.pause_requested = False
            self._condition.notify_all()

    def save_and_end(self):
        with self._condition:
            if not self.is_paused or self.best is None or self.quality_held:
                return
            self.pause_action = "best"
            self.pause_requested = False
            self._condition.notify_all()

    def stop(self):
        self.cancel.set()
        with self._condition:
            self._condition.notify_all()

    def _check_stop(self):
        if self.cancel.is_set():
            raise InterruptedError("Stopped; current settings retained")
        if self.progressive and self.progressive.baseline is not None:
            self._check_live_quality()

    def _check_live_quality(self):
        samples = {n: self.transport.sample(ICT_PVS[n]) for n in ('LN0', 'DR')}
        now = time.monotonic()
        for name, sample in samples.items():
            if not math.isfinite(sample.scalar()) or sample.scalar() <= 0:
                raise QualityTrip(f"Missing, invalid or non-positive {name} monitor value")
            if now - sample.received > self.config.ict_max_age:
                raise QualityTrip(f"{name} has no fresh notification within {self.config.ict_max_age:g} s")
        # A monitor ratio is an operational quality indicator, not a calibrated
        # same-pulse transmission or a measurement of radiation / beam loss.
        ratio = samples['DR'].scalar() / samples['LN0'].scalar()
        floor = self.progressive.baseline * self.config.min_transmission_fraction
        if not math.isfinite(ratio) or ratio < floor:
            raise QualityTrip(f"DR/LN0={ratio:g} below initial-ratio threshold {floor:g}")

    def _range_event(self, reason):
        state = self.progressive.snapshot()
        self.journal.write('exploration_range', reason=reason, **state)
        self.event('exploration_range', reason=reason, **state)

    def _quality_hold(self, error):
        self.progressive.shrink_and_limit()
        self._range_event('quality_hold_shrink')
        with self._condition:
            self.quality_held = self.is_paused = self.pause_requested = True
            details = dict(reason=str(error), step_id=self.visits + 1,
                           last_commanded_state=dict(self.state),
                           ict={n: dict(value=self.transport.sample(p).scalar(),
                                        timestamp=self.transport.sample(p).timestamp,
                                        received=self.transport.sample(p).received)
                                for n, p in ICT_PVS.items()},
                           beam_inhibit_sent=False)
            self.journal.write('quality_hold', **details)
            self.event('quality_hold', **details)
            while self.pause_requested and not self.cancel.is_set():
                self._condition.wait(.2)
            self.quality_held = self.is_paused = False
            if self.cancel.is_set():
                raise InterruptedError('Stopped during quality hold; no beam inhibit was sent')
            self.journal.write('quality_recheck_requested')

    def _guard(self, changed_at):
        start = time.monotonic()
        within_since = None
        pending = []
        while time.monotonic() - start <= self.config.arrival_timeout:
            self._check_stop()
            pending = []
            for name, item in self.items.items():
                a, target = item.actuator, self.state[name]
                try:
                    measured = scalar(self.transport, a.read) if a.read else command_value(a, self.transport)
                    fresh = not a.read or self.transport.sample(a.read).received >= changed_at.get(name, 0)
                    if item.error(measured, target) > item.tolerance + 1e-9 or not fresh:
                        pending.append(name)
                    for pv, target_pv in item.encode(target).items():
                        if abs(scalar(self.transport, pv) - target_pv) > 1e-6:
                            pending.append(name)
                except ValueError:
                    pending.append(name)
            now = time.monotonic()
            if pending:
                within_since = None
            else:
                if (changed_at and self.progressive and self.progressive.baseline is not None
                        and any(self.transport.sample(ICT_PVS[n]).received < max(changed_at.values())
                                for n in ('LN0', 'DR'))):
                    self.transport.wait(.02)
                    continue
                if within_since is None:
                    within_since = now
                    self.event("guard", seconds=self.config.guard_seconds)
                if now - within_since >= self.config.guard_seconds:
                    return
            self.transport.wait(.02)
        raise RuntimeError("Arrival timeout / external setting change: " + ", ".join(dict.fromkeys(pending)))

    def _apply(self, targets, kind, exact_restore=False):
        for name, target in targets.items():
            self.items[name].validate_target(target, self.transport, exact_restore)
        # Bound each physical command, including reference/best/initial returns.
        # Wait for arrival and new ICT monitor data at each intermediate step.
        while True:
            self._check_stop()
            waypoint = {}
            for name, target in targets.items():
                item = self.items[name]
                current = command_value(item.actuator, self.transport)
                if item.actuator.kind == 'phase':
                    current += 360 * round((self.state[name] - current) / 360)
                self.state[name] = current
                limit = item.step * self.config.max_step_steps
                waypoint[name] = (target if abs(target - current) <= limit + 1e-9 else
                                  current + math.copysign(limit, target - current))
            final = all(abs(waypoint[n] - v) < 1e-9 for n, v in targets.items())
            self._apply_once(waypoint, kind if final else kind + '_transition',
                             exact_restore=exact_restore if final else True)
            if final:
                return

    def _apply_once(self, targets, kind, exact_restore=False):
        # Validate the entire vector before the first write, then each command
        # again against current limit notifications before sending it.
        for name, target in targets.items():
            self.items[name].validate_target(target, self.transport, exact_restore)
        self.journal.write("step_request", step_id=self.visits + 1, random_index=self.random_index, kind=kind, targets=targets)
        self.event("writing", kind=kind, targets=targets)
        changed_at = {}
        for name, target in targets.items():
            item = self.items[name]
            for pv, value in item.encode(target).items():
                self._check_stop()
                item.validate_target(target, self.transport, exact_restore)
                before = scalar(self.transport, pv)
                if abs(before - value) <= 1e-9:
                    continue
                if item.error(before, value) > item.step * self.config.max_step_steps + 1e-8:
                    raise RuntimeError(f"Setting changed before command / step limit exceeded: {pv}")
                command_id = uuid.uuid4().hex
                # Persist intent before a physical side effect. If the process
                # dies between intent/ack, the command outcome is explicitly unknown.
                self.journal.write("command_intent", command_id=command_id, step_id=self.visits + 1,
                                   kind=kind, name=name, pv=pv, before=before, requested=value,
                                   logical_target=target, exact_restore=exact_restore)
                changed_at[name] = time.monotonic()
                self.touched.add(pv)
                self._point_changed.add(name)
                try:
                    self.transport.put(pv, value, timeout=2.)
                except Exception as exc:
                    self.journal.write("command_error", command_id=command_id, error=str(exc), outcome="unknown")
                    raise
                self.journal.write("command_ack", command_id=command_id)
                self.event("command", name=name, pv=pv, target=value)
            self.state[name] = target
        self.event("arrival", kind=kind)
        self._guard(changed_at)
        self.journal.write("arrival_confirmed", step_id=self.visits + 1,
                           feedback={n: p.actuator.feedback for n, p in self.items.items()})

    def _measure(self, kind, targets):
        self.event("measuring", kind=kind, samples=self.config.samples)
        start_ns, start = time.time_ns(), time.monotonic()
        self.journal.write("measurement_started", step_id=self.visits + 1, kind=kind,
                           measurement_start_utc_ns=start_ns)
        cursors = {k: self.transport.sample(p).sequence for k, p in ICT_PVS.items()}
        last_time = {k: self.transport.sample(p).timestamp for k, p in ICT_PVS.items()}
        values = {k: [] for k in ICT_PVS}
        times = {k: [] for k in ICT_PVS}
        while time.monotonic() - start < self.config.measurement_timeout:
            self._check_stop()
            for name, item in self.items.items():
                for pv, expected in item.encode(self.state[name]).items():
                    if abs(scalar(self.transport, pv) - expected) > 1e-6:
                        raise RuntimeError(f"Setting changed during measurement: {pv}")
                if item.actuator.read:
                    if item.error(scalar(self.transport, item.actuator.read), self.state[name]) > item.tolerance + 1e-9:
                        raise RuntimeError(f"Readback left tolerance during measurement: {name}")
            for key, pv in ICT_PVS.items():
                for sample in self.transport.history(pv, cursors[key]):
                    cursors[key] = sample.sequence
                    value = sample.scalar()
                    if (sample.received >= start and sample.timestamp > last_time[key]
                            and math.isfinite(value) and len(values[key]) < self.config.samples):
                        values[key].append(value)
                        times[key].append(sample.timestamp)
                        last_time[key] = sample.timestamp
            if all(len(v) >= self.config.samples for v in values.values()):
                break
            self.transport.wait(.02)
        means = {k: float(np.mean(v)) if v else math.nan for k, v in values.items()}
        counts = {k: len(v) for k, v in values.items()}
        valid = all(n == self.config.samples for n in counts.values())
        # Beam loss / negative signals are measurements, not connection failures.
        # They remain logged; only a physically positive score qualifies as best.
        score = means[self.config.score_ict]
        if not valid or not (means["LN0"] > 0 and score > 0):
            score = math.nan
        end_ns = time.time_ns()
        self.visits += 1
        quality = "complete" if valid else "incomplete_fresh_ICT_samples"
        payload = dict(step_id=self.visits, kind=kind, measurement_start_utc_ns=start_ns,
                       measurement_end_utc_ns=end_ns, quality=quality, sample_counts=counts,
                       source_time_ranges={k: [min(t), max(t)] if t else None for k, t in times.items()},
                       score=score, dr_mode=self.transport.sample(DR_MODE).scalar(),
                       dr_turn=self.transport.sample(DR_TURN).scalar(),
                       dr_waveform_timestamp=self.transport.sample(DR_MONITORS).timestamp)
        if self.config.save_ict_summary:
            payload["ict_means"] = means
        self.journal.write("measurement_window", **payload)
        self.journal.point(dict(step_id=self.visits, kind=kind, measurement_start_utc_ns=start_ns,
            measurement_end_utc_ns=end_ns, quality=quality, sample_counts=json.dumps(counts),
            score=score if math.isfinite(score) else "", requested_names=",".join(targets),
            ict_means_optional=json.dumps(clean_json(means)) if self.config.save_ict_summary else ""))
        first_baseline = self.progressive.baseline is None
        ratio = self.progressive.check_means(means, valid)
        self.journal.write('quality_evaluation', step_id=self.visits, ratio=ratio,
                           baseline_ratio=self.progressive.baseline,
                           minimum_ratio=self.progressive.baseline * self.config.min_transmission_fraction)
        if first_baseline:
            self._range_event('initial_baseline')
        if math.isfinite(score) and (self.best is None or score > self.best["score"]):
            self.best = dict(score=score, visit=self.visits, settings=dict(self.state))
            self.journal.write("best_observed", **self.best)
        self.event("point", **{**payload, "ict_means": means, "settings": dict(self.state)})

    def _pause_checkpoint(self):
        if not self.pause_requested:
            return None
        with self._condition:
            self.is_paused = True
            self.journal.write("paused", step_id=self.visits, settings=self.state)
            self.event("paused")
            while self.pause_requested and not self.cancel.is_set():
                self._condition.wait(.2)
            self.is_paused = False
            self._check_stop()
            action = self.pause_action
            self.journal.write("resumed" if action == "resume" else "save_and_end_requested", step_id=self.visits)
            return action

    def run(self):
        self.running = True
        status, error = "complete", ""
        try:
            self.config.validate()
            if not self.selected or len({a.name for a in self.selected}) != len(self.selected):
                raise ValueError("Select one or more unique actuators")
            self.items = {a.name: prepare(a, self.transport, self.config, self.overrides.get(a.name)) for a in self.selected}
            self.progressive = ProgressiveRange(self.items, self.config)
            self.state = {n: p.origin for n, p in self.items.items()}
            for pv in ICT_PVS.values():
                scalar(self.transport, pv)
            self.journal = Journal(self.root, dict(config=asdict(self.config), demo=self.transport.is_demo,
                actuators={n: asdict(p) for n, p in self.items.items()}, ict_pvs=ICT_PVS,
                raw_measurements_saved=False, dr_diagnostics_read_only=True,
                schema="random-excitation-commands/2", clock="UTC nanoseconds + ISO8601; local monotonic waits"))
            self.event("started", directory=str(self.journal.directory), prepared=self.items)
            self._range_event('initial_range')
            deadline = time.monotonic() + self.config.hours * 3600
            sampler = Sampler(self.items, self.config, self.progressive)
            targets, kind, exact = dict(self.state), "initial", True
            while True:
                try:
                    self._check_stop()
                    self._point_changed = set()
                    self._apply(targets, kind, exact_restore=exact)
                    self._measure(kind, targets)
                    if kind == 'expansion_reference':
                        if self.progressive.expand_after_reference():
                            self._range_event('expanded_after_reference')
                    else:
                        self.progressive.observe(kind, self._point_changed)
                    action = self._pause_checkpoint()
                    if action == "best":
                        self._apply(self.best["settings"], "save_and_end_best", exact_restore=True)
                        status = "saved_best"
                        break
                    if action == "resume":
                        targets, kind, exact = dict(self.state), "pause_remeasure", True
                        continue
                    if time.monotonic() >= deadline or (self.config.max_points and self.visits >= self.config.max_points):
                        if self.config.end_action == "initial":
                            self._apply({n: p.origin for n, p in self.items.items()}, "finish_initial", exact_restore=True)
                        elif self.config.end_action == "best":
                            if self.best is None:
                                raise RuntimeError("No valid best point available; current settings retained")
                            self._apply(self.best["settings"], "finish_best", exact_restore=True)
                        break
                    if self.progressive.ready_to_expand:
                        targets = {n: p.origin for n, p in self.items.items()}
                        kind, exact = 'expansion_reference', True
                    else:
                        self.random_index += 1
                        kind, targets = sampler.next(self.random_index)
                        exact = kind == "reference"
                except QualityTrip as exc:
                    if self.progressive.baseline is None:
                        raise  # No valid reference: no exploratory command is allowed.
                    self._quality_hold(exc)
                    # Live quality is rechecked BEFORE any recovery command.
                    targets = {n: p.origin for n, p in self.items.items()}
                    kind, exact = 'quality_recovery', True
        except QualityTrip as exc:
            status, error = "quality_stopped", str(exc)
            if self.journal:
                self.journal.write('quality_stop', reason=str(exc), beam_inhibit_sent=False)
        except InterruptedError as exc:
            status, error = "stopped", str(exc)
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            self.running = False
            if self.journal:
                try:
                    self.journal.write("session_end", status=status, error=error, visits=self.visits,
                                       best=self.best, last_commanded_state=self.state, touched_pvs=sorted(self.touched))
                except Exception as exc:
                    status, error = "error", f"Journal finalization failed: {exc}; {error}"
                finally:
                    try:
                        self.journal.close()
                    except Exception as exc:
                        status, error = "error", f"Journal close failed: {exc}; {error}"
            self.event("finished", result=status, error=error,
                       directory=str(self.journal.directory) if self.journal else "")
        return status
