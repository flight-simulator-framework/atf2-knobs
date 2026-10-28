"""Progressive exploration around a fixed reference, independent of hardware IO."""
from dataclasses import replace
import math


class QualityTrip(RuntimeError):
    """An exploration-quality condition failed; this is not a beam interlock."""


class ProgressiveRange:
    def __init__(self, items, config):
        self.items, self.config = items, config
        self.stage = 0
        radius = max(max(p.origin - p.lower, p.upper - p.origin) /
                     (config.initial_radius_steps * p.step) for p in items.values())
        self.max_stage = max(0, math.ceil(math.log2(max(1., radius))))
        self.baseline = None
        self.reset_evidence()
        for p in items.values():
            if not p.lower <= p.origin <= p.upper:
                raise ValueError(f"{p.actuator.name}: reference must be inside Lower/Upper")

    def reset_evidence(self):
        self.passed = 0
        self.coverage = {name: 0 for name in self.items}

    def active(self):
        result = {}
        for name, p in self.items.items():
            radius = p.step * self.config.initial_radius_steps * 2 ** self.stage
            result[name] = replace(p, lower=max(p.lower, p.origin - radius),
                                   upper=min(p.upper, p.origin + radius))
        return result

    def snapshot(self):
        return dict(stage=self.stage, max_stage=self.max_stage, passed=self.passed,
                    coverage=dict(self.coverage), baseline_ratio=self.baseline,
                    minimum_ratio=None if self.baseline is None else
                    self.baseline * self.config.min_transmission_fraction,
                    ranges={n: [p.lower, p.upper] for n, p in self.active().items()})

    def check_means(self, means, complete):
        if not complete:
            raise QualityTrip("Incomplete fresh ICT samples; expansion is disabled")
        upstream, downstream = means['LN0'], means['DR']
        if not all(math.isfinite(v) and v > 0 for v in (upstream, downstream)):
            raise QualityTrip("Positive LN0 and DR measurements are required")
        ratio = downstream / upstream
        if not math.isfinite(ratio):
            raise QualityTrip("Invalid DR/LN0 ratio")
        if self.baseline is None:
            self.baseline = ratio
        elif ratio < self.baseline * self.config.min_transmission_fraction:
            raise QualityTrip(f"DR/LN0={ratio:g} is below {self.baseline * self.config.min_transmission_fraction:g}")
        return ratio

    def observe(self, kind, changed):
        if kind in ('initial', 'reference', 'expansion_reference', 'quality_recovery', 'pause_remeasure'):
            return
        if changed:
            self.passed += 1
            for name in changed:
                self.coverage[name] += 1

    @property
    def ready_to_expand(self):
        return (self.stage < self.max_stage and self.passed >= self.config.expansion_points
                and all(n >= self.config.expansion_coverage for n in self.coverage.values()))

    def expand_after_reference(self):
        if not self.ready_to_expand:
            return False
        self.stage += 1
        self.reset_evidence()
        return True

    def shrink_and_limit(self):
        # Do not repeatedly probe a failed outer stage in the same session.
        self.stage = max(0, self.stage - 1)
        self.max_stage = self.stage
        self.reset_evidence()
