"""Calendar allocation from the workbook's ten-day periods, without runtime Excel dependencies."""
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
import json
import math

MIN_SEASON_DAYS = 30


@lru_cache(maxsize=1)
def reference_calendar():
    return json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))


def reference_date(crop, md, year):
    return date.fromisoformat(f"{year}-{md.replace('/', '-')}")


def map_date(crop, md, planted, harvested):
    """Affine allocation; half-day ties round forward, identically to the browser."""
    ref = reference_calendar()["crops"][crop]
    start = reference_date(crop, ref["planting_date"], planted.year)
    end = reference_date(crop, ref["harvest_date"], planted.year)
    offset = (reference_date(crop, md, planted.year) - start).days
    days = math.floor(offset * (harvested - planted).days / (end - start).days + 0.5)
    return planted + timedelta(days=days)


def allocate_stages(crop, planted, harvested):
    ref = reference_calendar()["crops"][crop]
    stages = []
    for row in ref["stages"]:
        # Rice nursery precedes transplanting and is outside this field simulation.
        if row["reference_date"] < ref["planting_date"]:
            continue
        stages.append(dict(row, date=map_date(crop, row["reference_date"], planted, harvested).isoformat()))
    return stages


def active_reference_date(crop, md, year):
    from config.simulation_settings import get_active
    settings = get_active(crop)
    if not settings:
        return reference_date(crop, md, year)
    if settings.get("stage_days"):
        # Interpolate every management reference date between the edited stages.
        points = settings["calendar_stages"]
        target = reference_date(crop, md, year)
        for left, right in zip(points, points[1:]):
            a = reference_date(crop, left["reference_date"], year)
            b = reference_date(crop, right["reference_date"], year)
            if a <= target <= b:
                x, y = date.fromisoformat(left["date"]), date.fromisoformat(right["date"])
                return x + timedelta(days=math.floor((target-a).days * (y-x).days / (b-a).days + .5))
    return map_date(crop, md, date.fromisoformat(settings["planting_date_iso"]),
                    date.fromisoformat(settings["harvest_date"]))


def adjusted_crop_params(crop, params):
    """Scale calibrated calendar events/durations along with the requested field season.

    If the user supplied explicit day-after-planting values (settings['phenology']),
    those win over the proportional rescaling for the four DAP milestones.
    """
    from config.simulation_settings import get_active
    settings = get_active(crop)
    if not settings:
        return params
    planted = date.fromisoformat(settings["planting_date_iso"])
    harvested = date.fromisoformat(settings["harvest_date"])
    ref = reference_calendar()["crops"][crop]
    baseline = (reference_date(crop, ref["harvest_date"], planted.year) -
                reference_date(crop, ref["planting_date"], planted.year)).days
    span = (harvested - planted).days
    ratio = span / baseline
    result = dict(params)
    user_stages = settings.get("phenology") or {}
    user_growth = settings.get("growth") or {}
    dap_keys = ("EmergenceCD", "MaxRootingCD", "SenescenceCD", "HIstartCD")
    for key in dap_keys:
        if key in user_stages:
            # Explicit DAP from the UI: use as-is, no proportional rescaling.
            result[key] = float(max(1, min(int(round(float(user_stages[key]))), span - 1)))
        else:
            result[key] = float(max(1, math.floor((params[key] - 1) * ratio + 0.5) + 1))
    for key in ("FloweringCD", "YldFormCD"):
        result[key] = float(max(1, math.floor(params[key] * ratio + 0.5)))
    result["MaturityCD"] = float(span)
    # Daily canopy growth/decline rates must follow the duration of the season too,
    # unless the user pinned them explicitly.
    for key in ("CGC_CD", "CDC_CD"):
        if key in user_growth:
            result[key] = float(user_growth[key])
        else:
            result[key] = params[key] / ratio
    return result
