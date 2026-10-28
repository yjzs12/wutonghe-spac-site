"""Depth averages of nodal soil predictions; no extrapolation or field calibration."""
from __future__ import annotations

from bisect import bisect_right
import math

FC_AGGREGATION = "piecewise_linear_depth_average"
FC_MAPPING_VERSION = "fc-depth-average-20261028"


def depth_average(depths_cm, values, top_cm, bottom_cm):
    """Integrate the piecewise-linear profile, then divide by layer thickness.

    Values retain their input units. Source depths are nodes, not layer widths.
    Missing/out-of-range data fail explicitly; no endpoint extrapolation.
    """
    depths = [float(z) for z in depths_cm]
    values = [float(v) for v in values]
    top, bottom = float(top_cm), float(bottom_cm)
    if len(depths) != len(values) or len(depths) < 2:
        raise ValueError("At least two paired depth/value nodes are required")
    if not all(math.isfinite(v) for v in depths + values + [top, bottom]):
        raise ValueError("Soil nodes and layer bounds must be finite")
    if any(b <= a for a, b in zip(depths, depths[1:])):
        raise ValueError("Source depths must be strictly increasing")
    if not depths[0] <= top < bottom <= depths[-1]:
        raise ValueError("Layer must lie within the observed/predicted depth range")

    def interpolate(z):
        i = min(bisect_right(depths, z) - 1, len(depths) - 2)
        fraction = (z - depths[i]) / (depths[i + 1] - depths[i])
        return values[i] + fraction * (values[i + 1] - values[i])

    cuts = [top] + [z for z in depths if top < z < bottom] + [bottom]
    integral = sum((b - a) * (interpolate(a) + interpolate(b)) / 2
                   for a, b in zip(cuts, cuts[1:]))
    return integral / (bottom - top)


def layer_mean_fc(records, layer_thicknesses_m):
    """Convert nodal volumetric FC percentages to model-layer m3/m3 means."""
    rows = sorted(records, key=lambda r: float(r["depth_cm"]))
    depths = [float(row["depth_cm"]) for row in rows]
    values = [float(row["field_capacity_pct"]) for row in rows]
    if any(not 0 < v < 100 for v in values):
        raise ValueError("FC source values must be volumetric percentages in (0,100)")
    result, top = [], 0.
    for thickness in layer_thicknesses_m:
        bottom = round(top + float(thickness) * 100, 8)
        result.append(round(depth_average(depths, values, top, bottom) / 100, 8))
        top = bottom
    return result
