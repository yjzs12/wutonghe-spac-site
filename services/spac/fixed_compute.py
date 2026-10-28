"""Offline fixed-regime worker. Each process owns its kernel and weather cache."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import zlib

REVISION = "fixed-regimes-v1-rainfed-20261028"
SCHEMES = {"rice": ["CF", "SWI", "AWD", "CI"],
           "corn": ["SMT100", "SMT75", "SMT50", "SMT25", "RAIN"]}
MANAGEMENT = dict(control_depth_m=.20, stop_days=7, regreen_days=17,
    tillering_peak_dap=33, drying_start_dap=48, drying_days=7, cold_events=[],
    ci_natural_drydown=True, ci_split_refill=True, ci_ponding_limit_days=None,
    ci_ponding_limit_depth_mm=None, ci_duration_drain_target_mm=None,
    awd_natural_drydown=True, awd_ponding_limit_days=None,
    awd_ponding_limit_depth_mm=None, awd_duration_drain_target_mm=None,
    pre_drying_days=3, drying_handoff=True, drying_min_days=5, recovery_budget_days=2)
DAILY_COLUMNS = ["模拟日期", "降雨量(mm)", "参考蒸散ET0(mm)",
    "第1层土壤含水率", "第2层土壤含水率", "第3层土壤含水率", "第4层土壤含水率",
    "田间水层深度(mm)", "每日灌溉量(mm)", "冠层覆盖度(%)", "蒸腾蒸发量(mm)",
    "干物质量(t/ha)", "籽粒产量(t/ha)", "水分利用效率(kg/m³)"]
_weather = {}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def initialize(kernel):
    sys.path.insert(0, str(kernel))
    import spac_model as sm
    original = sm.build_weather_df

    def weather(path, latitude, altitude):
        key = (str(Path(path).resolve()), latitude, altitude)
        if key not in _weather:
            _weather[key] = original(path, latitude, altitude)
        # A model may modify its dataframe: never return the cached object itself.
        return _weather[key].copy(deep=True)

    sm.build_weather_df = weather


def compute(item):
    """One grid/crop, all prescribed regimes; no optimizer or file exports."""
    import numpy as np
    import pandas as pd
    from config.simulation_settings import use_settings, validate_settings
    from modules.irrigation import get_smt_schemes, scheme_id
    from spac_model import run_simulation

    crop, year = item["crop"], item["year"]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            settings = validate_settings([crop], year, {crop: item["agronomy"]})
            options = [s for s in get_smt_schemes(crop)
                       if scheme_id(s[0]) in SCHEMES[crop]]
            assert [scheme_id(s[0]) for s in options] == SCHEMES[crop]
            records, daily = [], {}
            with use_settings(settings):
                for label, smt, mode, depths, kind in options:
                    result = run_simulation(crop, label, smt, mode=mode, depths=depths,
                        scheme_type=kind, sim_year=year, weather_csv=item["weather_path"],
                        application_efficiency_pct=90., rice_management=MANAGEMENT if crop == "rice" else None)
                    summary = result.summary
                    irr = float(summary["累计灌溉量(mm)"])
                    yld = float(summary["最终籽粒产量(t/ha)"])
                    et = float(summary["全季蒸腾蒸发量(mm)"])
                    if not all(math.isfinite(v) and v >= 0 for v in (irr, yld, et)):
                        raise ValueError("Invalid model result")
                    if mode == "rainfed" and abs(irr) > 1e-8:
                        raise ValueError("Rainfed must have zero irrigation")
                    sid = scheme_id(label)
                    record = dict(scheme=sid, label=label, mode=mode, smt=smt,
                        irrigation_mm=irr, yield_t_ha=yld, et_mm=et,
                        net_water_m3=item["area_ha"] * irr * 10,
                        production_t=item["area_ha"] * yld,
                        wue_kg_m3=float(summary.get("水分利用效率(kg/m³)", 0)))
                    records.append(record)
                    df = result.daily
                    cols = [c for c in DAILY_COLUMNS if c in df]
                    dates = pd.to_datetime(df["模拟日期"]).dt.strftime("%Y-%m-%d").tolist()
                    # Float32 is a display-storage representation only. All seasonal
                    # metrics/aggregation retain float64. No change to model arithmetic.
                    numeric = np.asarray(df[cols[1:]], dtype=np.float32)
                    if not np.isfinite(numeric).all():
                        raise ValueError("Nonfinite daily model output")
                    daily[sid] = dict(columns=cols, dates=dates,
                        shape=list(numeric.shape), data=numeric.tobytes().hex())
            public_input = dict(agronomy=settings[crop], rice_management=MANAGEMENT if crop == "rice" else None,
                application_efficiency_pct=90., weather_file=Path(item["weather_path"]).name,
                weather_sha256=item["weather_sha256"], point=item["point"],
                input_hash=item["input_hash"], daily_storage="float32; seasonal metrics float64")
            detail = dict(inputs=public_input, daily=daily)
            blob = zlib.compress(canonical(detail).encode(), 6)
            return dict(grid_id=item["grid_id"], crop=crop, year=year,
                point=item["point"], area_ha=item["area_ha"], input_hash=item["input_hash"],
                status="ok", schemes=records, detail=blob,
                output_hash=hashlib.sha256(blob).hexdigest())
    except Exception as exc:
        return dict(grid_id=item["grid_id"], crop=crop, year=year,
            point=item["point"], area_ha=item["area_ha"], input_hash=item["input_hash"],
            status="failed", error=f"{type(exc).__name__}: {exc}")


def unpack(blob, scheme=None):
    import struct
    detail = json.loads(zlib.decompress(blob))
    if scheme is None:
        return detail["inputs"]
    table = detail["daily"][scheme]
    raw = bytes.fromhex(table["data"])
    values = struct.unpack("<" + "f" * (len(raw) // 4), raw)
    width = table["shape"][1]
    return dict(columns=table["columns"], rows=[
        [day] + [round(v, 6) for v in values[i * width:(i + 1) * width]]
        for i, day in enumerate(table["dates"])])
