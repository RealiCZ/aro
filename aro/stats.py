"""The statistics behind the significance gate. stdlib-only, deterministic.

These are the small, load-bearing pieces of the judge: a robust central value
(median), a linear-interpolated quantile, and a seeded bootstrap CI. Everything
is deterministic given the seed so a re-run reproduces the same verdict.
"""
from __future__ import annotations

import math
import random


def median(values) -> float:
    v = sorted(x for x in values if _finite(x))
    n = len(v)
    if n == 0:
        return math.nan
    if n % 2 == 1:
        return v[n // 2]
    return (v[n // 2 - 1] + v[n // 2]) / 2.0


def quantile(values, q: float) -> float:
    """Linear-interpolated quantile at q in [0,1]. NaNs dropped."""
    v = sorted(x for x in values if _finite(x))
    n = len(v)
    if n == 0:
        return math.nan
    if n == 1:
        return v[0]
    q = min(max(q, 0.0), 1.0)
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if hi >= n:
        hi = n - 1
    if lo == hi:
        return v[lo]
    frac = pos - lo
    return v[lo] + (v[hi] - v[lo]) * frac


def seed_for_metric(metric: str) -> int:
    """Stable 64-bit seed per metric (FNV-1a-ish) so each bootstrap is
    reproducible and independent of metric ordering."""
    h = 0xCBF29CE484222325
    for b in metric.encode("utf-8"):
        h ^= b
        h = (h * 0x00000100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h ^ 0x9E3779B97F4A7C15


def bootstrap_ci(deltas_pct, iters: int = 2000, seed: int = 0):
    """~95% bootstrap CI (percent) on paired Δ% values; returns (low, high).

    Resamples with replacement `iters` times, takes each resample's mean, and
    returns the 2.5th / 97.5th percentiles of those means. Deterministic given
    `seed`. Empty input -> (0.0, 0.0)."""
    if not deltas_pct:
        return (0.0, 0.0)
    if iters == 0:
        m = sum(deltas_pct) / len(deltas_pct)
        return (m, m)
    rng = random.Random(seed)
    n = len(deltas_pct)
    means = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += deltas_pct[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    return (quantile(means, 0.025), quantile(means, 0.975))


def _finite(x) -> bool:
    try:
        return not (math.isnan(x) or math.isinf(x))
    except TypeError:
        return False
