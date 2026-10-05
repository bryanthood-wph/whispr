"""Scoring: design-weighted estimates, cluster bootstrap, Holm, the my-task bar and
the scorecard (docs/plan/B-eval.md B.6, B.7). Pure stdlib + numpy; no model calls.

Conventions (all functions below):

- A metric is a **ratio estimator** over one config's outcomes for that metric:
  sum(w * value) / sum(w * 1), one term per item, w = frame_n[cell] / sample n[cell].
- Outcomes whose unit is not in the design are ignored. That is how a filtered
  design (exclude_low_mic) scores the same outcome list.
- **Strata are domains.** A stratum (or the `units` argument of ratio/bootstrap) is an
  indicator on the per-unit numerator and denominator: units outside it contribute 0.
  Weights and resamples are the full design's, so a domain's size varies across
  replicates as it would on a redraw of the sample, and its variance includes that.
- **Zero denominator.** A ratio with no items in scope is NaN, and so is a bootstrap
  replicate whose resampled units carry no items in scope (for a domain: no domain
  unit carrying items was drawn). Such replicates stay in the list (so its length is
  always `replicates` and seeding stays deterministic) and are ignored by ci() and by
  the comparison p-value; this conditions the bootstrap on the replicate being
  defined. ci() raises if no replicate is defined. Scorecard entries report the
  dropped share as `nan_replicate_share`.
- **Bootstrap = Rao-Wu rescaling bootstrap** (Rao & Wu 1988, with n_h - 1 draws).
  In each cell of n_h units, n_h - 1 units are drawn with replacement and each
  drawn unit's count is scaled by n_h / (n_h - 1); a cell with n_h = 1 keeps its
  unit fixed (count 1). The naive "draw n_h of n_h" bootstrap understates the
  variance by (n_h - 1) / n_h, which matters for cells of 3-17 transcripts; with
  this rescaling the variance of a cell mean matches the unbiased s^2 / n_h.
  Counts are always drawn over the full design.units from numpy's default_rng(seed),
  so the same seed and units give the same resamples for every config, metric and
  stratum: estimates, strata and paired comparisons from one run share replicates.
- **One build per design.** ScorecardBuild indexes the outcomes once and draws the
  count matrix once per (replicates, seed); a scorecard should make one per design
  and call its methods. The module-level functions are one-off builds.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, fields
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from eval.records import Design, Outcome, Unit
from pipeline.config import DEFAULTS_PATH, _read_yaml, load_schema
from pipeline.jsonschema_lite import validate
from whispr.fileio import atomic_write_text

# A CI needs at least this many units carrying items in the domain, counting only
# units that Rao-Wu can resample (their cell has more than one unit): fewer give no
# between-cluster variance at all. A mathematical minimum, not a tunable. Below it
# the CI is null ("insufficient_units").
MIN_CLUSTERS_FOR_CI = 2

# Unit attributes a stratum can be cut on (B.7: cell, device, mic-coverage flag).
STRATA = tuple(f.name for f in fields(Unit) if f.name != "id")

# How a metric's outcome value is coded, which fixes the Rogan-Gladen direction.
# Rogan-Gladen estimates the prevalence of the class the judge is calibrated to
# detect: the ERROR (a miss, an unsupported claim, a violation).
CODING_HIT = "hit"      # value 1 = hit / present / supported (recall, faithfulness)
CODING_ERROR = "error"  # value 1 = the error itself (contradicted share, violations)

BOOTSTRAP, WILSON, INSUFFICIENT = "bootstrap", "wilson", "insufficient_units"


class ScorecardError(ValueError):
    """A scorecard failed config/schema/scorecard.json; nothing was written."""


@dataclass(frozen=True)
class Calibration:
    """B.5 calibration of the judge decision behind one metric. `coding` has no default:
    every caller states whether the metric's value 1 is a hit or an error."""
    sensitivity: Optional[float]
    specificity: Optional[float]
    coding: str

    def __post_init__(self):
        if self.coding not in (CODING_HIT, CODING_ERROR):
            raise ValueError(f"coding must be {CODING_HIT!r} or {CODING_ERROR!r}, got {self.coding!r}")


# --------------------------------------------------------------------------- core

def _divide(num, den):
    """num / den with NaN where den is 0 (works on scalars and arrays)."""
    num, den = np.asarray(num, dtype=float), np.asarray(den, dtype=float)
    out = np.full(np.broadcast(num, den).shape, np.nan)
    np.divide(num, den, out=out, where=den != 0)
    return out


def _resample_counts(units: Sequence[Unit], replicates: int, seed: int) -> np.ndarray:
    """(replicates, len(units)) Rao-Wu rescaled counts (see module doc): per cell,
    n_h - 1 draws with replacement, each count scaled by n_h / (n_h - 1)."""
    rng = np.random.default_rng(seed)
    counts = np.ones((replicates, len(units)))          # n_h = 1 cells stay fixed at 1
    cells: dict[str, list[int]] = {}
    for i, u in enumerate(units):
        cells.setdefault(u.cell, []).append(i)
    rows = np.arange(replicates)[:, None]
    for members in cells.values():                       # insertion order: deterministic
        n = len(members)
        if n == 1:
            continue
        draws = rng.integers(0, n, size=(replicates, n - 1))
        tally = np.bincount((draws + rows * n).ravel(), minlength=replicates * n)
        counts[:, members] = tally.reshape(replicates, n) * (n / (n - 1))
    return counts


def ci(replicates: Iterable[float], alpha: float) -> tuple[float, float]:
    """Percentile interval at level 1 - alpha, ignoring undefined (NaN) replicates.
    Endpoints are observed replicates (lower / higher order statistic, no interpolation),
    so an interval excludes 0 only if fewer than alpha/2 of the replicates lie beyond 0:
    it agrees with the bootstrap p-value of compare_family while the p floor
    1/(defined + 1) is below alpha/2. With fewer replicates the two can disagree."""
    arr = np.asarray(list(replicates), dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        raise ValueError("no defined bootstrap replicate")
    lo = np.percentile(arr, 100 * alpha / 2, method="lower")
    hi = np.percentile(arr, 100 * (1 - alpha / 2), method="higher")
    return float(lo), float(hi)


def wilson(k: float, n: float, alpha: float) -> tuple[float, float]:
    """Wilson score interval for k successes of n (unweighted small cells only, B.6)."""
    if n <= 0:
        raise ValueError("wilson needs n > 0")
    z = NormalDist().inv_cdf(1 - alpha / 2)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def rogan_gladen(obs: float, *, sens: float, spec: float) -> float:
    """(obs + spec - 1) / (sens + spec - 1), clipped to [0, 1] (B.5). `obs` is the
    observed prevalence of the class the judge detects with sensitivity `sens`."""
    youden = sens + spec - 1
    if youden <= 0:
        raise ValueError(f"sens + spec must exceed 1 to correct (got {sens} + {spec})")
    return min(1.0, max(0.0, (obs + spec - 1) / youden))


def corrected_rate(obs: float, *, sens: float, spec: float, coding: str) -> float:
    """Rogan-Gladen in the metric's direction: an error-coded rate is corrected as is;
    a hit-coded rate (recall, ...) is corrected through its error rate, 1 - RG(1 - obs)."""
    if coding == CODING_ERROR:
        return rogan_gladen(obs, sens=sens, spec=spec)
    if coding == CODING_HIT:
        return 1.0 - rogan_gladen(1.0 - obs, sens=sens, spec=spec)
    raise ValueError(f"coding must be {CODING_HIT!r} or {CODING_ERROR!r}, got {coding!r}")


# --------------------------------------------------------------- multiple testing

def _rank(pvalues: dict[str, float]) -> list[str]:
    """Names by ascending p; ties keep their given order (the pre-registered family order)."""
    return sorted(pvalues, key=lambda name: pvalues[name])


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values: monotone in rank, capped at 1."""
    m = len(pvalues)
    adjusted, running = {}, 0.0
    for k, name in enumerate(_rank(pvalues)):
        running = max(running, min(1.0, (m - k) * pvalues[name]))
        adjusted[name] = running
    return {name: adjusted[name] for name in pvalues}


def _holm_levels(pvalues: dict[str, float], alpha: float) -> dict[str, float]:
    """Per-hypothesis CI alpha for Holm-adjusted intervals (step-down, see compare_family):
    the k-th smallest p of m gets alpha / (m - k + 1) while Holm rejects; from the first
    non-rejection at rank r, it and all later ones get alpha / (m - r + 1). Keyed by
    name, in rank order."""
    m, p_holm = len(pvalues), holm(pvalues)
    levels, stop = {}, None
    for k, name in enumerate(_rank(pvalues)):
        if stop is None and p_holm[name] >= alpha:
            stop = k
        levels[name] = alpha / (m - (k if stop is None else stop))
    return levels


def _nullable(x: float) -> Optional[float]:
    return None if math.isnan(x) else float(x)


# ------------------------------------------------------------------ the analyses

def exclude_low_mic(design: Design) -> Design:
    """The design for the primary my-task analysis (B.3): low-mic units removed, frame_n
    kept, so the remaining units in a cell stand for the whole cell."""
    return Design(dict(design.frame_n), tuple(u for u in design.units if not u.low_mic))


def _usable(calibration: Calibration, cfg: dict) -> bool:
    """B.5: a correction needs both figures, sens + spec > 1, and sensitivity on target."""
    sens, spec = calibration.sensitivity, calibration.specificity
    return (sens is not None and spec is not None and sens + spec > 1
            and sens >= cfg["eval"]["calibration_min_sensitivity"])


def _point(estimate: float, lo: float, hi: float) -> dict:
    """JSON-safe {estimate, ci}: NaN becomes null."""
    return {"estimate": _nullable(estimate), "ci": [_nullable(lo), _nullable(hi)]}


class ScorecardBuild:
    """One design and one outcome list, prepared once for every estimate of a scorecard.

    - Design weights frame_n[cell] / sample n[cell] are computed once.
    - Outcomes are indexed once into per-unit arrays keyed by (config, metric), aligned
      with design.units: weighted numerator, weighted denominator and item count. Point
      estimates and replicates come from the same arrays.
    - The Rao-Wu count matrix is drawn once per (replicates, seed) and shared by every
      config, metric and stratum of the build. It is (replicates x units) floats, about
      10 MB at 10,000 replicates, so it lives only as long as the build.

    A filtered design (exclude_low_mic) is a different design: give it its own build.
    """

    def __init__(self, design: Design, outcomes: Iterable[Outcome]):
        self.design = design
        units = design.units
        n = len(units)
        pos = {u.id: i for i, u in enumerate(units)}
        sampled = Counter(u.cell for u in units)
        weights = np.array([design.frame_n[u.cell] / sampled[u.cell] for u in units], dtype=float)
        self._cells = [u.cell for u in units]
        self._resamplable = np.array([sampled[u.cell] > 1 for u in units], dtype=bool)
        index: dict[tuple[str, str], tuple[list[int], list[float]]] = {}
        for o in outcomes:
            i = pos.get(o.unit_id)
            if i is not None:
                at, values = index.setdefault((o.config, o.metric), ([], []))
                at.append(i)
                values.append(o.value)
        self._arrays: dict[tuple[str, str], tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for key, (at, values) in index.items():
            items = np.bincount(at, minlength=n).astype(float)
            self._arrays[key] = (np.bincount(at, weights=values, minlength=n) * weights, items * weights, items)
        self._empty = (np.zeros(n),) * 3
        self._counts: dict[tuple[int, int], np.ndarray] = {}
        self._strata: dict[str, dict[str, np.ndarray]] = {}

    # -- shared pieces

    def counts(self, replicates: int, seed: int) -> np.ndarray:
        """The design's Rao-Wu count matrix, drawn on first use and reused after."""
        key = (replicates, seed)
        if key not in self._counts:
            counts = _resample_counts(self.design.units, replicates, seed)
            counts.flags.writeable = False      # shared by every entry of this build
            self._counts[key] = counts
        return self._counts[key]

    def _mask(self, units: Optional[Sequence[Unit]]) -> Optional[np.ndarray]:
        """Domain indicator over design.units for `units` (None: the whole design)."""
        if units is None:
            return None
        ids = {u.id for u in units}
        return np.array([u.id in ids for u in self.design.units], dtype=bool)

    def _domain(self, config: str, metric: str, mask: Optional[np.ndarray]):
        """(num, den, items) per unit for one config's metric, 0 outside the domain."""
        arrays = self._arrays.get((config, metric), self._empty)
        return arrays if mask is None else tuple(a * mask for a in arrays)

    def _replicates(self, num: np.ndarray, den: np.ndarray, replicates: int, seed: int) -> np.ndarray:
        counts = self.counts(replicates, seed)
        return _divide(counts @ num, counts @ den)

    # -- estimates

    def ratio(self, config: str, metric: str, *, units: Optional[Sequence[Unit]] = None) -> float:
        """Design-weighted ratio estimate of one config's metric; NaN if no items. `units`
        restricts it to a domain (see module doc)."""
        num, den, _ = self._domain(config, metric, self._mask(units))
        return float(_divide(num.sum(), den.sum()))

    def bootstrap(self, config: str, metric: str, *, replicates: int, seed: int,
                  units: Optional[Sequence[Unit]] = None) -> np.ndarray:
        """Rao-Wu cluster bootstrap replicates of ratio() (see module doc, incl. NaN)."""
        num, den, _ = self._domain(config, metric, self._mask(units))
        return self._replicates(num, den, replicates, seed)

    def compare_family(self, family: Sequence[tuple[str, str]], metric: str, *,
                       replicates: int, seed: int, alpha: float) -> dict[str, dict[str, Any]]:
        """Paired bootstrap comparisons cfg_a - cfg_b on the same resampled units, Holm-adjusted.

        cfg_a is the newer config. Per comparison "A-B": estimate, ci_lo, ci_hi, ci_level,
        the two-sided bootstrap p (floored at 1 / (defined replicates + 1)), p_holm, win,
        loss and undefined.

        - Holm-adjusted CIs, step-down: in ascending p order, the k-th comparison (k = 1..m)
          gets level alpha / (m - k + 1) while Holm keeps rejecting; from the first
          non-rejection at rank r onward, every comparison uses alpha / (m - r + 1), so no
          CI excludes 0 while its p_holm >= alpha, provided the defined replicates exceed
          m / alpha (the p floor 1/(n + 1) sits below every level; at 10,000 replicates
          that holds for m < 500). Below that a CI can exclude 0 with p_holm >= alpha;
          win still requires p_holm < alpha, so it is never a win.
        - win: p_holm < alpha and the CI lies entirely above 0; loss: entirely below 0.
        - undefined: a config has no items, so the difference has no estimate or no
          defined replicate. Its numbers are null, p is 1 (it stays in the family, which
          keeps Holm conservative), and win/loss are False.
        """
        names = [f"{a}-{b}" for a, b in family]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate comparisons in family: {names}")
        point, reps = {}, {}
        for c in dict.fromkeys(c for pair in family for c in pair):
            num, den, _ = self._domain(c, metric, None)
            point[c] = float(_divide(num.sum(), den.sum()))
            reps[c] = self._replicates(num, den, replicates, seed)
        diffs, pvalues = {}, {}
        for name, (a, b) in zip(names, family):
            d = reps[a] - reps[b]
            d = d[~np.isnan(d)]
            estimate = point[a] - point[b]
            undefined = math.isnan(estimate) or d.size == 0
            diffs[name] = (estimate, d, undefined)
            if undefined:
                pvalues[name] = 1.0
            else:
                share = min(np.mean(d <= 0), np.mean(d >= 0))
                pvalues[name] = max(min(1.0, 2 * float(share)), 1 / (d.size + 1))
        p_holm = holm(pvalues)
        levels = _holm_levels(pvalues, alpha)
        result = {}
        for name in names:
            level = levels[name]
            estimate, d, undefined = diffs[name]
            lo, hi = (math.nan, math.nan) if undefined else ci(d, level)
            rejected = not undefined and p_holm[name] < alpha
            result[name] = {"estimate": _nullable(estimate), "ci_lo": _nullable(lo), "ci_hi": _nullable(hi),
                            "ci_level": 1 - level, "p": pvalues[name], "p_holm": p_holm[name],
                            "win": rejected and lo > 0, "loss": rejected and hi < 0, "undefined": undefined}
        return result

    # -- scorecard entries

    def metric_entry(self, config: str, metric: str, *, cfg: dict,
                     calibration: Optional[Calibration] = None) -> dict:
        """Scorecard entry for one config's metric over the whole design. `calibration` is the
        B.5 calibration of the metric's judge decision (None: no calibrated error type)."""
        return self._summarize(config, metric, None, cfg, calibration)

    def stratum_entries(self, config: str, metric: str, *, by: str, cfg: dict,
                        calibration: Optional[Calibration] = None) -> dict[str, dict]:
        """Scorecard entries per value of one stratum (`by` in STRATA: cell, device, low_mic),
        keyed by the value as a string ("True"/"False" for low_mic). Each stratum is a
        domain of the whole design: the full design's weights and resamples, with units
        outside the stratum contributing 0 (see module doc)."""
        if by not in STRATA:
            raise ValueError(f"unknown stratum {by!r}; expected one of {STRATA}")
        if by not in self._strata:
            values = [str(getattr(u, by)) for u in self.design.units]
            self._strata[by] = {v: np.array([x == v for x in values], dtype=bool) for v in dict.fromkeys(values)}
        return {value: self._summarize(config, metric, mask, cfg, calibration)
                for value, mask in self._strata[by].items()}

    def _summarize(self, config: str, metric: str, mask: Optional[np.ndarray], cfg: dict,
                   calibration: Optional[Calibration]) -> dict:
        """One scorecard metric entry for a domain (`mask`; None: the whole design).

        - CI method: "insufficient_units" (CI null) unless at least MIN_CLUSTERS_FOR_CI
          resamplable units of the domain carry items; else "wilson" when the domain is
          a single cell (equal weights) of at most eval.scoring.wilson_max_units units;
          else "bootstrap".
        - Wilson counts clusters, not items: n = units with items, k = n * estimate, so
          several items from one transcript are not treated as independent draws.
        - `nan_replicate_share`: share of bootstrap replicates undefined and dropped
          (null when no bootstrap ran).
        - Rogan-Gladen: applied in the calibration's coding direction. The corrected CI
          maps the raw CI's endpoints, so it ignores uncertainty in the calibration
          figures. Unusable calibration (see _usable) gives corrected null, unreliable true.
        """
        ev = cfg["eval"]
        alpha = ev["alpha"]
        num, den, items = self._domain(config, metric, mask)
        in_domain = np.ones(len(self._cells), dtype=bool) if mask is None else mask
        with_items = items > 0
        n_with_items = int(np.count_nonzero(with_items))
        estimate = float(_divide(num.sum(), den.sum()))
        nan_share = lo = hi = math.nan
        if np.count_nonzero(with_items & self._resamplable) < MIN_CLUSTERS_FOR_CI:
            method = INSUFFICIENT
        elif (len({c for c, inside in zip(self._cells, in_domain) if inside}) == 1
              and np.count_nonzero(in_domain) <= ev["scoring"]["wilson_max_units"]):
            method = WILSON                                  # one cell: weights are equal
            lo, hi = wilson(n_with_items * estimate, n_with_items, alpha)
        else:
            method = BOOTSTRAP
            reps = self._replicates(num, den, ev["bootstrap_replicates"], ev["seed"])
            nan_share = float(np.mean(np.isnan(reps)))
            lo, hi = ci(reps, alpha)
        corrected, unreliable = None, False
        if calibration is not None:
            if _usable(calibration, cfg):
                fix = (lambda x: x if math.isnan(x) else corrected_rate(
                    x, sens=calibration.sensitivity, spec=calibration.specificity, coding=calibration.coding))
                corrected = _point(fix(estimate), fix(lo), fix(hi))
            else:
                unreliable = True
        return {"raw": _point(estimate, lo, hi), "corrected": corrected, "unreliable": unreliable,
                "ci_method": method, "n_units": int(np.count_nonzero(in_domain)),
                "n_units_with_items": n_with_items, "n_items": int(items.sum()),
                "nan_replicate_share": _nullable(nan_share)}


# One-off builds: each call indexes `outcomes` afresh. A scorecard calls the methods of
# one ScorecardBuild per design instead, so the counts and the index are shared.

def ratio(design: Design, outcomes: Iterable[Outcome], config: str, metric: str, *,
          units: Optional[Sequence[Unit]] = None) -> float:
    """ScorecardBuild.ratio on a one-off build."""
    return ScorecardBuild(design, outcomes).ratio(config, metric, units=units)


def bootstrap(design: Design, outcomes: Iterable[Outcome], config: str, metric: str, *,
              replicates: int, seed: int, units: Optional[Sequence[Unit]] = None) -> list[float]:
    """ScorecardBuild.bootstrap on a one-off build, as a list."""
    return ScorecardBuild(design, outcomes).bootstrap(config, metric, replicates=replicates, seed=seed,
                                                      units=units).tolist()


def compare_family(design: Design, outcomes: Iterable[Outcome], family: Sequence[tuple[str, str]],
                   metric: str, *, replicates: int, seed: int, alpha: float) -> dict[str, dict[str, Any]]:
    """ScorecardBuild.compare_family on a one-off build."""
    return ScorecardBuild(design, outcomes).compare_family(family, metric, replicates=replicates,
                                                           seed=seed, alpha=alpha)


def metric_entry(design: Design, outcomes: Iterable[Outcome], config: str, metric: str, *,
                 cfg: dict, calibration: Optional[Calibration] = None) -> dict:
    """ScorecardBuild.metric_entry on a one-off build."""
    return ScorecardBuild(design, outcomes).metric_entry(config, metric, cfg=cfg, calibration=calibration)


def stratum_entries(design: Design, outcomes: Iterable[Outcome], config: str, metric: str, *,
                    by: str, cfg: dict, calibration: Optional[Calibration] = None) -> dict[str, dict]:
    """ScorecardBuild.stratum_entries on a one-off build."""
    return ScorecardBuild(design, outcomes).stratum_entries(config, metric, by=by, cfg=cfg,
                                                            calibration=calibration)


# ------------------------------------------------------------- sizing (B.3, pilot)

def icc(groups: Sequence[Sequence[float]]) -> Optional[float]:
    """One-way ANOVA intraclass correlation ICC(1) of item outcomes clustered by
    transcript, truncated at 0. Unequal cluster sizes use the average size n0 =
    (N - sum n_i^2 / N) / (k - 1). Empty clusters are dropped (a transcript with no
    items adds nothing). None when it is undefined: fewer than 2 clusters, no
    within-cluster degrees of freedom (N = k), or no variance at all."""
    groups = [[float(x) for x in g] for g in groups if len(g)]
    k, n = len(groups), sum(len(g) for g in groups)
    if k < 2 or n <= k:
        return None
    grand = sum(sum(g) for g in groups) / n
    means = [sum(g) / len(g) for g in groups]
    ssb = sum(len(g) * (m - grand) ** 2 for g, m in zip(groups, means))
    ssw = sum((x - m) ** 2 for g, m in zip(groups, means) for x in g)
    if ssb + ssw == 0:
        return None
    msb, msw = ssb / (k - 1), ssw / (n - k)
    n0 = (n - sum(len(g) ** 2 for g in groups) / n) / (k - 1)
    return max(0.0, (msb - msw) / (msb + (n0 - 1) * msw))


def transcripts_needed(*, rho: Optional[float], tasks_per_transcript: float, min_effective: int) -> Optional[int]:
    """Transcripts needed so the effective number of items reaches `min_effective`
    (B.3: eval.min_effective_tasks). A transcript of m correlated items is worth
    m / (1 + (m - 1) rho) independent ones (the design effect). The design effect is
    clamped at 1: for m < 1 it would fall below 1 and credit a transcript with more
    effective items than it has. m is the mean items per transcript, so variation in
    cluster size (which raises the design effect) is ignored. None when there is
    nothing to size from: no rho, or m <= 0."""
    m = tasks_per_transcript
    if rho is None or m <= 0:
        return None
    return math.ceil(min_effective / (m / max(1.0, 1 + (m - 1) * rho)))


def _at_least(name: str, value: Optional[float], threshold: float, reasons: list[str]) -> bool:
    """value >= threshold; a missing (None/NaN) value fails. Failures add a reason."""
    if value is None or math.isnan(value):
        reasons.append(f"{name} is undefined")
        return False
    if value < threshold:
        reasons.append(f"{name} {value:.4f} < {threshold}")
        return False
    return True


def my_task_bar(*, recall: Optional[float], recall_lb: Optional[float], precision: Optional[float],
                unexplained_misses: int, cfg: dict, recall_lb_method: str = BOOTSTRAP) -> dict[str, Any]:
    """B.6 my-task bar: all four criteria must hold. Thresholds from eval.bar.

    A missing (None/NaN) figure fails its criterion. The recall lower bound must come
    from the cluster bootstrap (B.6): any other `recall_lb_method` (a Wilson bound,
    "insufficient_units") fails criterion 2. The default is "bootstrap" so the plain
    call stays valid; pass the metric entry's ci_method to make the source explicit.
    Returns pass, one bool per criterion, recall_lb_method and the failure reasons.
    """
    bar = cfg["eval"]["bar"]
    reasons: list[str] = []
    recall_ok = _at_least("recall", recall, bar["my_task_recall"], reasons)
    lb_ok = _at_least("recall lower bound", recall_lb, bar["my_task_recall_lower_bound"], reasons)
    if recall_lb_method != BOOTSTRAP:
        reasons.append(f"recall lower bound came from {recall_lb_method!r}, not the cluster bootstrap")
        lb_ok = False
    precision_ok = _at_least("precision", precision, bar["my_task_precision"], reasons)
    if unexplained_misses:
        reasons.append(f"{unexplained_misses} miss(es) not root-caused")
    checks = {"recall": recall_ok, "recall_lower_bound": lb_ok, "precision": precision_ok,
              "misses_root_caused": unexplained_misses == 0}
    return {"pass": all(checks.values()), **checks, "recall_lb_method": recall_lb_method, "reasons": reasons}


# -------------------------------------------------------------------- scorecard

def _schema(cfg: Optional[dict]) -> dict:
    if cfg is None:                                      # the schema path is not per-user
        cfg = _read_yaml(DEFAULTS_PATH)
    return load_schema(cfg, "scorecard")


def write_scorecard(path: Path, scorecard: dict, *, cfg: Optional[dict] = None) -> None:
    """Validate against config/schema/scorecard.json, then write atomically. Raises
    ScorecardError (and writes nothing) on a schema violation or a NaN/inf value."""
    errors = validate(scorecard, _schema(cfg))
    if errors:
        raise ScorecardError("scorecard failed the schema:\n  " + "\n  ".join(errors[:20]))
    try:
        text = json.dumps(scorecard, ensure_ascii=False, indent=1, allow_nan=False)
    except ValueError as exc:
        raise ScorecardError(f"scorecard holds a non-JSON number: {exc}") from None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)
